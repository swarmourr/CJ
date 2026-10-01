"""Paper-ready grayscale-safe PDF/PNG plot generation.

All plots fail clearly with a descriptive notice when no real results
exist — no placeholder numbers are generated.

Requires matplotlib (optional dependency):
    pip install matplotlib

If matplotlib is not installed, all plot functions return None and print
a notice instead of raising.
"""

from __future__ import annotations

import os
from typing import Any


def _check_matplotlib() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        print("[eval/plots] matplotlib not installed — skipping plot generation.")
        print("  Install with: pip install matplotlib")
        return False


def _grayscale_style() -> dict:
    """Return rcParams for grayscale-safe paper figures."""
    return {
        "figure.figsize":     (6, 4),
        "axes.prop_cycle":    _gray_cycle(),
        "axes.facecolor":     "white",
        "axes.edgecolor":     "black",
        "axes.grid":          True,
        "grid.color":         "#cccccc",
        "grid.linestyle":     "--",
        "font.size":          10,
        "axes.titlesize":     11,
        "axes.labelsize":     10,
        "xtick.labelsize":    9,
        "ytick.labelsize":    9,
        "legend.fontsize":    9,
        "figure.dpi":         150,
        "savefig.dpi":        300,
        "savefig.bbox":       "tight",
    }


def _gray_cycle():
    import matplotlib
    colors = ["#000000", "#555555", "#999999", "#000000"]
    hatches = [None, "//", "xx", ".."]
    return matplotlib.cycler(color=colors)


def _no_data_notice(path: str, reason: str) -> None:
    print(f"[eval/plots] No data: {reason}. Skipping {path}.")
    # Write a one-line text notice file so the path exists in output
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path.replace(".pdf", ".txt").replace(".png", ".txt"), "w") as f:
        f.write(f"NO DATA: {reason}\n")


def plot_degradation(
    condition_summaries: list[dict],
    output_path: str = "results/figures/degradation.pdf",
) -> str | None:
    """Bar chart of pass@1 degradation per fault type.

    Parameters
    ----------
    condition_summaries : list[dict]
        Each dict must have keys: ``fault_type``, ``pass_at_1_baseline``,
        ``pass_at_1_fault``, ``n_fault_valid``.
    output_path : str

    Returns
    -------
    str | None
        Path to the saved figure, or None if skipped.
    """
    if not _check_matplotlib():
        return None

    def _flt(v: Any) -> float | None:
        if v is None or v == "" or v == "None":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    real = [
        c for c in condition_summaries
        if _flt(c.get("pass_at_1_baseline")) is not None
        and _flt(c.get("pass_at_1_fault")) is not None
        and int(c.get("n_fault_valid", 0) or 0) > 0
    ]
    if not real:
        _no_data_notice(output_path, "no valid fault records with pass@1 data")
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with plt.rc_context(_grayscale_style()):
        fig, ax = plt.subplots()
        labels = [c["fault_type"] for c in real]
        degrad = [
            round(_flt(c["pass_at_1_baseline"]) - _flt(c["pass_at_1_fault"]), 3)
            for c in real
        ]
        x = range(len(labels))
        hatches = ["//" if d > 0 else "" for d in degrad]
        bars = ax.bar(x, degrad, color=["#444444"] * len(labels), edgecolor="black")
        for bar, h in zip(bars, hatches):
            bar.set_hatch(h)
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=30, ha="right")
        ax.set_ylabel("Degradation (baseline − fault pass@1)")
        ax.set_title("Success Rate Degradation under Fault")
        ax.axhline(0, color="black", linewidth=0.8)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        plt.savefig(output_path)
        plt.close()
    return output_path


def plot_validity_summary(
    records: list[dict],
    output_path: str = "results/figures/validity.pdf",
) -> str | None:
    """Stacked bar chart showing validity breakdown per fault type."""
    if not _check_matplotlib():
        return None

    fault_records = [r for r in records if r.get("phase") == "fault"]
    if not fault_records:
        _no_data_notice(output_path, "no fault-phase records")
        return None

    from collections import defaultdict
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {
        "valid": 0, "invalid": 0, "inconclusive": 0, "untriggered": 0
    })
    for r in fault_records:
        ft = r.get("fault_type", "unknown")
        v  = r.get("validity", "inconclusive")
        counts[ft][v] += 1

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = list(counts.keys())
    cats   = ["valid", "inconclusive", "invalid", "untriggered"]
    grays  = ["#111111", "#555555", "#999999", "#cccccc"]

    with plt.rc_context(_grayscale_style()):
        fig, ax = plt.subplots()
        bottoms = [0] * len(labels)
        for cat, gray in zip(cats, grays):
            vals = [counts[l][cat] for l in labels]
            ax.bar(range(len(labels)), vals, bottom=bottoms, label=cat,
                   color=gray, edgecolor="black")
            bottoms = [b + v for b, v in zip(bottoms, vals)]
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right")
        ax.set_ylabel("Run count")
        ax.set_title("Injection Validity per Fault Type")
        ax.legend(loc="upper right")

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        plt.savefig(output_path)
        plt.close()
    return output_path


def plot_amplification(
    condition_summaries: list[dict],
    metric: str = "llm_call_amplification",
    output_path: str = "results/figures/amplification.pdf",
) -> str | None:
    """Bar chart of call/token/duration amplification factors."""
    if not _check_matplotlib():
        return None

    def _flt2(v: Any) -> float | None:
        if v is None or v == "" or v == "None":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    real = [
        c for c in condition_summaries
        if _flt2(c.get(metric)) is not None
    ]
    if not real:
        _no_data_notice(output_path, f"no records with {metric}")
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with plt.rc_context(_grayscale_style()):
        fig, ax = plt.subplots()
        labels = [c["fault_type"] for c in real]
        vals   = [_flt2(c[metric]) for c in real]
        ax.bar(range(len(labels)), vals, color="#333333", edgecolor="black")
        ax.axhline(1.0, color="black", linewidth=0.8, linestyle="--", label="baseline (1.0×)")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right")
        ax.set_ylabel(f"{metric.replace('_', ' ')}")
        ax.set_title(f"Amplification: {metric}")
        ax.legend()

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        plt.savefig(output_path)
        plt.close()
    return output_path
