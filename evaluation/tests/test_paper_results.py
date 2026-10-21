from __future__ import annotations

import csv
import json
from pathlib import Path

from evaluation.paper_results import discover_studies


def _study(
    root: Path,
    category: str,
    name: str,
    *,
    level: str = "individual",
    system: str = "autogen-real",
    topology: str = "single",
    benchmark: str = "humanevalplus",
    records: int = 3,
) -> Path:
    directory = root / category / name
    directory.mkdir(parents=True)
    with (directory / "scientific_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["agent_level", "system", "topology", "benchmark"],
        )
        writer.writeheader()
        writer.writerow({
            "agent_level": level,
            "system": system,
            "topology": topology,
            "benchmark": benchmark,
        })
    with (directory / "runs.jsonl").open("w", encoding="utf-8") as handle:
        for index in range(records):
            handle.write(json.dumps({"run": index}) + "\n")
    return directory


def test_discovery_keeps_experiment_families_separate(tmp_path: Path) -> None:
    main = tmp_path / "main"
    recovery = tmp_path / "recovery"
    injector = _study(main, "injector-validation", "injector")
    resilience = _study(main, "individual-resilience-pilot", "resilience")

    selected, decisions = discover_studies(main, recovery)

    assert set(selected) == {injector, resilience}
    assert sum(bool(row["included"]) for row in decisions) == 2


def test_discovery_prefers_largest_repaired_langgraph_study(tmp_path: Path) -> None:
    main = tmp_path / "main"
    recovery = tmp_path / "recovery"
    common = {
        "level": "multi_agent",
        "system": "langgraph-real",
        "topology": "linear",
    }
    _study(main, "native-multi-agent-pilot", "pre-repair", records=324, **common)
    _study(recovery, "native-multi-agent-pilot", "smoke", records=9, **common)
    repaired = _study(recovery, "native-multi-agent-pilot", "repaired", records=324, **common)

    selected, decisions = discover_studies(main, recovery)

    assert selected == [repaired]
    included = [row for row in decisions if row["included"]]
    assert len(included) == 1
    assert included[0]["source_campaign"] == "recovery"
