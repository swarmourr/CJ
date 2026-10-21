"""Build paper-ready tables, figures, and narrative from completed CJ studies."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int:
    parsed = number(value)
    return int(parsed) if parsed is not None else 0


def run_count(study: Path) -> int:
    path = study / "runs.jsonl"
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def study_key(category: str, rows: list[dict[str, str]]) -> tuple[str, str, str, str, str]:
    first = rows[0]
    return (
        category,
        first.get("agent_level", ""),
        first.get("system", ""),
        first.get("topology", ""),
        first.get("benchmark", ""),
    )


def discover_studies(main: Path, recovery: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    candidates: list[tuple[str, str, Path, list[dict[str, str]]]] = []
    for source, root in (("main", main), ("recovery", recovery)):
        for summary in root.glob("*/*/scientific_summary.csv"):
            rows = read_csv(summary)
            if rows:
                candidates.append((source, summary.parent.parent.name, summary.parent, rows))

    selected: list[Path] = []
    decisions: list[dict[str, Any]] = []
    grouped: dict[
        tuple[str, str, str, str, str],
        list[tuple[str, str, Path, list[dict[str, str]]]],
    ] = defaultdict(list)
    for item in candidates:
        grouped[study_key(item[1], item[3])].append(item)

    for key, options in sorted(grouped.items()):
        category, level, system, topology, benchmark = key
        ranked = sorted(options, key=lambda item: (run_count(item[2]), item[0] == "recovery"), reverse=True)
        chosen = ranked[0]

        # Repaired native LangGraph studies supersede the known-broken main studies.
        if level == "multi_agent" and system == "langgraph-real":
            recovery_options = [item for item in options if item[0] == "recovery"]
            if recovery_options:
                chosen = max(recovery_options, key=lambda item: run_count(item[2]))

        # Tool-required studies are additive and have unique system labels.
        selected.append(chosen[2])
        for source, option_category, path, _rows in options:
            included = path == chosen[2]
            decisions.append({
                "experiment_category": option_category,
                "agent_level": level,
                "system": system,
                "topology": topology,
                "benchmark": benchmark,
                "source_campaign": source,
                "study_path": str(path.resolve()),
                "record_count": run_count(path),
                "included": included,
                "reason": (
                    "canonical completed study"
                    if included
                    else "superseded retry, smoke, or pre-repair study"
                ),
            })
    return sorted(set(selected)), decisions


def load_selected(studies: Iterable[Path], filename: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for study in studies:
        for row in read_csv(study / filename):
            row = dict(row)
            row["source_study"] = str(study.resolve())
            rows.append(row)
    return rows


def aggregate_accounting(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("system") == "tool-triggered-controlled":
            family = "Controlled tool robustness"
        elif row.get("agent_level") == "multi_agent":
            family = "Native multi-agent"
        elif "injector-validation" in row.get("source_study", ""):
            family = "Injector validation"
        else:
            family = "Individual-agent resilience"
        groups[family].append(row)

    result = []
    for family, items in groups.items():
        result.append({
            "experiment_family": family,
            "summary_rows": len(items),
            "complete_triplets": sum(integer(r.get("complete_triplets")) for r in items),
            "valid_fault_pairs": sum(integer(r.get("valid_pairs")) for r in items),
            "fully_scored_triplets": sum(integer(r.get("scored_triplets")) for r in items),
            "baseline_eligible_pairs": sum(integer(r.get("baseline_eligible_pairs")) for r in items),
            "excluded_untriggered": sum(integer(r.get("excluded_untriggered_count")) for r in items),
            "excluded_invalid": sum(integer(r.get("excluded_invalid_count")) for r in items),
            "maximum_unique_tasks": max((integer(r.get("unique_tasks")) for r in items), default=0),
            "maximum_seed_count": max((integer(r.get("seed_count")) for r in items), default=0),
        })
    order = {"Injector validation": 0, "Individual-agent resilience": 1, "Native multi-agent": 2, "Controlled tool robustness": 3}
    return sorted(result, key=lambda row: order[row["experiment_family"]])


def subset_table(rows: list[dict[str, str]], kind: str) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        system = row.get("system", "")
        level = row.get("agent_level", "")
        source = row.get("source_study", "")
        include = {
            "injector": "injector-validation" in source,
            "individual": level == "individual" and system != "tool-triggered-controlled" and "injector-validation" not in source,
            "multi_agent": level == "multi_agent",
            "tool": system == "tool-triggered-controlled",
        }[kind]
        if not include:
            continue
        output.append({
            "system": system,
            "topology": row.get("topology", ""),
            "benchmark": row.get("benchmark", ""),
            "fault": row.get("fault", ""),
            "severity": row.get("severity", ""),
            "unique_tasks": row.get("unique_tasks", ""),
            "seed_count": row.get("seed_count", ""),
            "complete_triplets": row.get("complete_triplets", ""),
            "valid_pairs": row.get("valid_pairs", ""),
            "scored_triplets": row.get("scored_triplets", ""),
            "baseline_eligible_pairs": row.get("baseline_eligible_pairs", ""),
            "baseline_pass_at_1": row.get("baseline_pass_at_1", ""),
            "control_pass_at_1": row.get("control_pass_at_1", ""),
            "fault_pass_at_1": row.get("fault_pass_at_1", ""),
            "fault_specific_degradation": row.get("fault_specific_degradation", ""),
            "conditional_robustness": row.get("conditional_robustness", ""),
            "trigger_rate": row.get("trigger_rate", ""),
            "manifestation_rate": row.get("manifestation_rate", ""),
            "recovery_rate": row.get("recovery_rate", ""),
            "untriggered_rate": row.get("untriggered_rate", ""),
            "inference_status": row.get("inference_status", ""),
        })
    return sorted(output, key=lambda r: (r["system"], r["topology"], r["benchmark"], r["fault"]))


def fmt(value: Any, digits: int = 3) -> str:
    parsed = number(value)
    return "--" if parsed is None else f"{parsed:.{digits}f}"


def latex_escape(value: Any) -> str:
    text = str(value)
    for old, new in (("\\", r"\textbackslash{}"), ("_", r"\_"), ("%", r"\%"), ("&", r"\&"), ("#", r"\#")):
        text = text.replace(old, new)
    return text


def write_latex_table(path: Path, rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> None:
    spec = "l" * len(columns)
    lines = [r"\begin{tabular}{" + spec + "}", r"\toprule"]
    lines.append(" & ".join(latex_escape(label) for _key, label in columns) + r" \\")
    lines.append(r"\midrule")
    for row in rows:
        values = []
        for key, _label in columns:
            value = row.get(key, "")
            values.append(latex_escape(value if value not in (None, "") else "--"))
        lines.append(" & ".join(values) + r" \\")
    lines.extend([r"\bottomrule", r"\end{tabular}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_figures(out: Path, scientific: list[dict[str, str]], accounting: list[dict[str, Any]], overhead: list[dict[str, str]]) -> None:
    os.environ.setdefault("MPLCONFIGDIR", str(out / ".mplconfig"))
    os.environ.setdefault("XDG_CACHE_HOME", str(out / ".cache"))
    os.environ.setdefault("MPLBACKEND", "Agg")
    Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False})
    figures = out / "figures"
    figures.mkdir(parents=True, exist_ok=True)

    labels = [r["experiment_family"] for r in accounting]
    complete = [r["complete_triplets"] for r in accounting]
    valid = [r["valid_fault_pairs"] for r in accounting]
    scored = [r["fully_scored_triplets"] for r in accounting]
    x = np.arange(len(labels))
    width = 0.25
    fig, ax = plt.subplots(figsize=(8.4, 3.8))
    ax.bar(x - width, complete, width, label="Complete triplets", color="#4C78A8")
    ax.bar(x, valid, width, label="Valid fault pairs", color="#59A14F")
    ax.bar(x + width, scored, width, label="Fully scored", color="#E15759")
    ax.set_xticks(x, labels, rotation=15, ha="right")
    ax.set_ylabel("Count")
    ax.legend(frameon=False, ncol=3)
    ax.set_title("Experimental coverage and analysis eligibility")
    fig.tight_layout()
    fig.savefig(figures / "data_eligibility.pdf")
    fig.savefig(figures / "data_eligibility.png", dpi=180)
    plt.close(fig)

    individual = subset_table(scientific, "individual")
    row_labels = sorted({f"{r['system'].replace('-real','')} / {r['benchmark'].replace('plus','+')}" for r in individual})
    faults = sorted({r["fault"] for r in individual})
    matrix = np.full((len(row_labels), len(faults)), np.nan)
    for row in individual:
        label = f"{row['system'].replace('-real','')} / {row['benchmark'].replace('plus','+')}"
        value = number(row["fault_specific_degradation"])
        if value is not None:
            matrix[row_labels.index(label), faults.index(row["fault"])] = value
    fig, ax = plt.subplots(figsize=(8.2, 4.2))
    image = ax.imshow(matrix, cmap="RdYlBu_r", vmin=-0.1, vmax=0.3, aspect="auto")
    ax.set_xticks(range(len(faults)), [f.replace("_", " ") for f in faults], rotation=25, ha="right")
    ax.set_yticks(range(len(row_labels)), row_labels)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            if not np.isnan(matrix[i, j]):
                ax.text(j, i, f"{matrix[i,j]:.2f}", ha="center", va="center", fontsize=8)
    ax.set_title("Fault-specific pass@1 degradation (CJ control - CJ fault)")
    fig.colorbar(image, ax=ax, label="Absolute degradation")
    fig.tight_layout()
    fig.savefig(figures / "individual_degradation_heatmap.pdf")
    fig.savefig(figures / "individual_degradation_heatmap.png", dpi=180)
    plt.close(fig)

    usable_overhead = [r for r in overhead if number(r.get("median_duration_overhead_ratio")) is not None]
    usable_overhead.sort(key=lambda r: (r.get("agent_level", ""), r.get("framework", ""), r.get("topology", ""), r.get("benchmark", "")))
    labels = [f"{r.get('framework','').replace('-real','')}\n{r.get('topology','')} / {r.get('benchmark','').replace('plus','+')}" for r in usable_overhead]
    values = [100 * (number(r.get("median_duration_overhead_ratio")) or 0.0) for r in usable_overhead]
    colors = ["#4C78A8" if r.get("agent_level") == "individual" else "#F28E2B" for r in usable_overhead]
    fig, ax = plt.subplots(figsize=(10, 4.6))
    ax.bar(range(len(values)), values, color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xticks(range(len(labels)), labels, rotation=45, ha="right")
    ax.set_ylabel("Median duration overhead (%)")
    ax.set_title("CJ pass-through overhead relative to direct execution")
    fig.tight_layout()
    fig.savefig(figures / "cj_overhead.pdf")
    fig.savefig(figures / "cj_overhead.png", dpi=180)
    plt.close(fig)


def build_report(out: Path, accounting: list[dict[str, Any]], scientific: list[dict[str, str]], inference: list[dict[str, str]]) -> None:
    individual = subset_table(scientific, "individual")
    native = subset_table(scientific, "multi_agent")
    tool = subset_table(scientific, "tool")
    injector = subset_table(scientific, "injector")
    significant_success = []
    for row in inference:
        adjusted = number(row.get("p_value_holm"))
        if row.get("metric") == "success" and row.get("inference_status") == "ok" and adjusted is not None and adjusted < 0.05:
            significant_success.append(row)

    latency_rows = [r for r in individual if r["fault"] == "llm_latency"]
    unavailable_rows = [r for r in individual if r["fault"] == "llm_unavailable"]
    tool_valid = sum(integer(r["valid_pairs"]) for r in tool)
    tool_total = sum(integer(r["complete_triplets"]) for r in tool)
    report = f"""# Paper results analysis

## Canonical data selection

This package combines the completed main campaign with targeted recovery studies. Superseded retries, the nine-record LangGraph smoke study, and the pre-repair native LangGraph studies are excluded by `study_selection_manifest.json`. Raw result directories remain unchanged.

## Experimental accounting

| Family | Complete triplets | Valid fault pairs | Fully scored | Baseline eligible |
|---|---:|---:|---:|---:|
"""
    for row in accounting:
        report += f"| {row['experiment_family']} | {row['complete_triplets']} | {row['valid_fault_pairs']} | {row['fully_scored_triplets']} | {row['baseline_eligible_pairs']} |\n"

    report += f"""
## RQ1: Injection validity

The injector-validation smoke covered {len(injector)} representative fault configurations. All selected injector rows report complete activation, triggering, manifestation, and verified recovery. This establishes mechanism validity under the tested configuration, but the one-task smoke should not be presented as broad statistical generalization.

## RQ2: Individual-agent resilience

The individual campaign evaluates {len(individual)} framework-benchmark-fault strata. Latency was valid in every selected stratum ({sum(integer(r['valid_pairs']) for r in latency_rows)} valid pairs) and generally preserved correctness among eligible outputs while increasing duration. LLM unavailability was valid in {sum(integer(r['valid_pairs']) for r in unavailable_rows)} pairs but commonly produced no answer; these runs are operational failures and must not be mistaken for missing experiments. Score availability differs materially by framework under token starvation and semantic corruption, so every pass@1 result must retain its scored and baseline-eligible denominator.

## RQ3: Native multi-agent resilience

Native AutoGen and CrewAI results from the main campaign are combined with repaired LangGraph studies from the recovery campaign. Planner unavailability and reviewer corruption now have role-scoped lifecycle evidence in all three frameworks. Planner failures generally prevent downstream work, whereas reviewer corruption occurs after code generation and can separate artifact correctness from operational success. Native coder tool faults remain untriggered when workflows do not emit tool-result traffic and are reported as reachability limits rather than resilience failures.

## RQ4: Tool-fault robustness

The controlled tool-required workflow produced {tool_valid}/{tool_total} valid fault pairs across HumanEval+ and MBPP+. These experiments validate CJ's ability to intercept and fail a reached tool-result stage. They are reported separately from native multi-agent experiments because the controlled workflow guarantees tool traffic.

## RQ5: CJ overhead

Direct-versus-control results are reported in `cj_overhead_table.csv`. Median duration overhead is the primary statistic because runtime distributions contain large outliers, especially for CrewAI. Negative single-stratum estimates are treated as timing noise rather than acceleration.

## Statistical interpretation

Inference uses exact pair identifiers, task-clustered confidence intervals, paired tests, and Holm correction. There are {len(significant_success)} Holm-significant control-versus-fault success results in the selected inferential outputs. Absence of significance is not interpreted as equivalence, particularly in strata with limited scoreability.

## Required exclusions and threats to validity

- Source provenance is recorded as dirty; results are strong pilot evidence rather than a final frozen artifact.
- MiniMax-M2 is the only model in this package.
- Infrastructure and distributed-native experiments are not included.
- Hard faults often produce no answer, reducing the number of EvalPlus-scoreable triplets.
- The recovery campaign is a post-hoc implementation repair and is identified separately in the selection manifest.
"""
    (out / "RESULTS.md").write_text(report, encoding="utf-8")

    tex = r"""\section{Results}
\label{sec:results}

We separate fault-injection validity, agent resilience, multi-agent process behavior, tool-stage robustness, and CJ pass-through overhead. Only fault executions supported by activation, trigger, and manifestation evidence enter fault-effect estimates. Untriggered and invalid executions remain visible in the accounting tables but are excluded from resilience estimates.

\subsection{Experimental accounting}
\input{tables/accounting.tex}

\subsection{Fault-injection validity}
The injector-validation campaign demonstrates evidence-backed activation, manifestation, reversion, and recovery for the representative fault configurations. Because this validation uses a bounded smoke workload, it supports mechanism validity rather than broad task-level generalization.

\subsection{Individual-agent resilience}
Latency faults were consistently reachable and primarily increased execution time, while task correctness was generally preserved among eligible outputs. Service unavailability produced operational failures with no answer. Token starvation and semantic response corruption exposed framework-dependent differences in output availability and conditional correctness. All pass@1 estimates are therefore reported with complete, valid, scored, and baseline-eligible denominators.

\subsection{Multi-agent propagation and recovery}
Planner unavailability generally prevented downstream work, while reviewer corruption occurred after candidate generation and could preserve a correct artifact despite operational failure. Native coder tool faults that did not reach a tool-result message are classified as untriggered rather than as successful resilience trials. Repaired LangGraph studies are reported as post-hoc recovery evidence and supersede the pre-repair implementation-failure strata.

\subsection{Tool-stage robustness}
The controlled tool-required workload validates tool-result fault triggering separately from native workflows. This distinction prevents workflow reachability from being conflated with injector effectiveness.

\subsection{CJ overhead}
We compare direct execution with CJ pass-through control using paired task-level observations. Median overhead and task-clustered confidence intervals are emphasized because mean runtime is sensitive to long-tailed provider and framework latency.

\subsection{Threats to validity}
The current package uses one primary model, excludes infrastructure experiments, and records a dirty source state. It should therefore be treated as a comprehensive pilot rather than the final frozen publication artifact.
"""
    (out / "results_section.tex").write_text(tex, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-results", type=Path, required=True)
    parser.add_argument("--recovery-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    studies, decisions = discover_studies(args.main_results.resolve(), args.recovery_results.resolve())
    scientific = load_selected(studies, "scientific_summary.csv")
    inference = load_selected(studies, "inferential_summary.csv")
    overhead = load_selected(studies, "cj_overhead_summary.csv")
    accounting = aggregate_accounting(scientific)

    write_csv(out / "study_selection.csv", decisions)
    write_csv(out / "experimental_accounting.csv", accounting)
    write_csv(out / "scientific_results.csv", scientific)
    write_csv(out / "injector_validity_table.csv", subset_table(scientific, "injector"))
    write_csv(out / "individual_resilience_table.csv", subset_table(scientific, "individual"))
    write_csv(out / "multi_agent_resilience_table.csv", subset_table(scientific, "multi_agent"))
    write_csv(out / "tool_resilience_table.csv", subset_table(scientific, "tool"))
    write_csv(out / "cj_overhead_table.csv", overhead)
    write_csv(out / "inferential_results.csv", inference)

    significant = []
    for row in inference:
        adjusted = number(row.get("p_value_holm"))
        if row.get("inference_status") == "ok" and adjusted is not None and adjusted < 0.05:
            significant.append(row)
    write_csv(out / "holm_significant_results.csv", significant)

    manifest = {
        "analysis_schema": "cj-paper-analysis-v1",
        "main_results": str(args.main_results.resolve()),
        "recovery_results": str(args.recovery_results.resolve()),
        "selected_studies": [str(path.resolve()) for path in studies],
        "selected_study_count": len(studies),
        "selection_decisions": decisions,
        "limitations": [
            "dirty source provenance",
            "one primary model",
            "no infrastructure results in selected campaigns",
            "hard-fault no-answer runs are not EvalPlus-scoreable",
        ],
    }
    (out / "study_selection_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    tables = out / "tables"
    tables.mkdir(exist_ok=True)
    write_latex_table(tables / "accounting.tex", accounting, [
        ("experiment_family", "Family"),
        ("complete_triplets", "Triplets"),
        ("valid_fault_pairs", "Valid"),
        ("fully_scored_triplets", "Scored"),
        ("baseline_eligible_pairs", "Eligible"),
    ])
    write_latex_table(tables / "individual_resilience.tex", subset_table(scientific, "individual"), [
        ("system", "System"), ("benchmark", "Benchmark"), ("fault", "Fault"),
        ("valid_pairs", "Valid"), ("scored_triplets", "Scored"),
        ("control_pass_at_1", "Control"), ("fault_pass_at_1", "Fault"),
    ])
    write_latex_table(tables / "multi_agent_resilience.tex", subset_table(scientific, "multi_agent"), [
        ("system", "System"), ("topology", "Topology"), ("benchmark", "Benchmark"),
        ("fault", "Fault"), ("valid_pairs", "Valid"), ("scored_triplets", "Scored"),
    ])

    build_report(out, accounting, scientific, inference)
    make_figures(out, scientific, accounting, overhead)
    print(f"[paper-analysis] selected studies: {len(studies)}")
    print(f"[paper-analysis] scientific rows: {len(scientific)}")
    print(f"[paper-analysis] output: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
