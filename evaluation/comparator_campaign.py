"""External fault-injector comparator availability gate.

The paper design includes a CJ-vs-external-injector comparison, but that is only
scientifically valid when the external injector is actually installed and used.
This module records an explicit comparator status instead of silently skipping or
claiming unsupported evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.schema import OUTPUT_SCHEMA_VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--study-id")
    parser.add_argument("--comparator", choices=["toxiproxy"], default="toxiproxy")
    return parser


def _version(cmd: str, *args: str) -> dict[str, Any]:
    path = shutil.which(cmd)
    if not path:
        return {"available": False, "path": "", "version": "", "error": "not found"}
    proc = subprocess.run(
        [path, *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return {
        "available": proc.returncode == 0,
        "path": path,
        "version": (proc.stdout or proc.stderr).strip().splitlines()[0:3],
        "exit_code": proc.returncode,
    }


def _toxiproxy_status() -> dict[str, Any]:
    server = _version("toxiproxy-server", "--version")
    cli = _version("toxiproxy-cli", "--version")
    ready = bool(server.get("available") and cli.get("available"))
    return {
        "comparator": "toxiproxy",
        "ready": ready,
        "server": server,
        "cli": cli,
        "required_next_steps": [] if ready else [
            "install toxiproxy-server",
            "install toxiproxy-cli",
            "run a matched CJ-vs-Toxiproxy latency/network campaign",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    study_id = args.study_id or f"comparator-study-{uuid.uuid4().hex[:12]}"
    status = _toxiproxy_status()
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "timestamp": timestamp,
        "study_id": study_id,
        "campaign_id": f"comparator-{args.comparator}",
        "pair_id": "",
        "run_id": f"comparator-{uuid.uuid4().hex[:16]}",
        "condition": "comparator_preflight",
        "phase": "preflight",
        "agent_level": "comparator",
        "agent_system": "none",
        "framework": "none",
        "topology": "none",
        "benchmark": "none",
        "task_id": "comparator/availability",
        "model": "none",
        "fault_type": "external_comparator",
        "fault_layer": "comparator",
        "validity": "valid" if status["ready"] else "unsupported",
        "lifecycle": {
            "configured": True,
            "activated": status["ready"],
            "triggered": False,
            "manifested": False,
            "reverted": True,
            "recovered": True,
            "verdict": "valid" if status["ready"] else "unsupported",
            "evidence_source": "comparator_binary_probe",
        },
        "success": 1.0 if status["ready"] else 0.0,
        "reported_error": 0.0 if status["ready"] else 1.0,
        "executor_status": "ok" if status["ready"] else "unsupported",
        "comparator_status": status,
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
    }
    (results_dir / "comparator_status.json").write_text(
        json.dumps(record, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    with (results_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    with (results_dir / "comparator_summary.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "study_id",
                "comparator",
                "ready",
                "server_path",
                "cli_path",
                "validity",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "study_id": study_id,
            "comparator": status["comparator"],
            "ready": status["ready"],
            "server_path": status["server"].get("path", ""),
            "cli_path": status["cli"].get("path", ""),
            "validity": record["validity"],
        })
    print(f"[cj-eval] comparator gate complete: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
