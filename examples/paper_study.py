"""
Chaos-Jungle Paper Study — E1 through E9
=========================================

Reproduces the nine-experiment chaos engineering study from:

  Rynge et al., "Chaos Engineering for Scientific Workflows",
  PEARC '19, Chicago, IL, USA.

The study tests a Rosetta protein-design workflow running under Pegasus WMS
and HTCondor across nine conditions — from a clean baseline to fully
compounded chaos.

Workflow under test
-------------------
  10 x .pdb files (protein structures)
       │
       ▼
  HTCondor distributes 10 jobs across worker nodes
       │
       ▼
  minirosetta: reads .pdb → computes redesign → writes score + output .pdb
       │
       ▼
  Pegasus collects results at submit node

Chaos targets
-------------
  - Network faults  : tc netem on the worker's network interface
  - Storage faults  : cj_storage bit-flips .pdb files at block-device level
  - Silent corrupt  : BPF swaps payload bytes while preserving TCP checksums

Usage
-----
  # Edit the WORKER_* and SUBMIT_* variables below, then:
  python examples/paper_study.py

  # Run a specific experiment by name:
  python examples/paper_study.py --experiment E3-net-delay

  # Dry-run (print plan, do not connect to targets):
  python examples/paper_study.py --dry-run
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── chaos-jungle imports ──────────────────────────────────────────────────────
from chaos_jungle import Scenario, ExperimentSuite
from chaos_jungle.faults import (
    NetworkDelay,
    NetworkLoss,
    NetworkCorrupt,
    NetworkDuplicate,
    StorageCorrupt,
)
from chaos_jungle.targets import SSHTarget, LocalTarget


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these for your cluster
# ═══════════════════════════════════════════════════════════════════════════════

# The HTCondor worker node where chaos is injected.
# For a multi-node study, add WORKER_2, WORKER_3, etc. and assign each
# experiment to a different target (see paper_study.yml for a parallel version).
WORKER_HOST = os.environ.get("CJ_WORKER_HOST", "worker1.example.com")
WORKER_USER = os.environ.get("CJ_WORKER_USER", "ubuntu")
WORKER_KEY  = os.environ.get("CJ_WORKER_KEY",  "~/.ssh/id_rsa")

# The Pegasus submit node (where you run pegasus-plan / pegasus-run).
# Leave as None to run workflow commands on your local machine.
SUBMIT_HOST = os.environ.get("CJ_SUBMIT_HOST", None)
SUBMIT_USER = os.environ.get("CJ_SUBMIT_USER", "ubuntu")

# Path to the Pegasus workflow DAX/YAML on the submit node.
WORKFLOW_DIR = os.environ.get("CJ_WORKFLOW_DIR", "/home/ubuntu/rosetta-workflow")

# Directory on the worker where .pdb input files live.
PDB_DIR      = os.environ.get("CJ_PDB_DIR", "/scratch/rosetta/input")

# Results are saved here (local machine).
RESULTS_DIR  = Path(os.environ.get("CJ_RESULTS_DIR", "./results"))

# How long to run each experiment.
# The workflow finishes when all 10 Rosetta jobs complete.
# If the workflow takes longer than this, chaos stops but the workflow continues.
EXPERIMENT_DURATION = os.environ.get("CJ_DURATION", "30m")


# ═══════════════════════════════════════════════════════════════════════════════
# TARGETS
# ═══════════════════════════════════════════════════════════════════════════════

def make_worker() -> SSHTarget:
    """SSH target pointing at the HTCondor worker node."""
    return SSHTarget(
        host=WORKER_HOST,
        user=WORKER_USER,
        key=os.path.expanduser(WORKER_KEY),
    )


def make_submit():
    """Return the submit target (local or SSH)."""
    if SUBMIT_HOST:
        return SSHTarget(host=SUBMIT_HOST, user=SUBMIT_USER,
                         key=os.path.expanduser(WORKER_KEY))
    return LocalTarget()


# ═══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT DEFINITIONS  (E1 – E9)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Each entry is a dict:
#   name     : short identifier, also used as the results sub-directory
#   label    : human-readable title
#   question : what this experiment is designed to reveal
#   faults   : list of Fault instances (empty for baseline)
#
# All experiments share the same worker target and the same duration.
# ─────────────────────────────────────────────────────────────────────────────

EXPERIMENTS = [

    # ── E1  Baseline ─────────────────────────────────────────────────────────
    {
        "name":     "E1-baseline",
        "label":    "Baseline — no chaos",
        "question": "Normal behavior. Reference point for all other experiments.",
        "faults":   [],
    },

    # ── E2  Storage corruption ────────────────────────────────────────────────
    {
        "name":     "E2-storage-only",
        "label":    "Storage corruption only",
        "question": "Can Pegasus detect silent disk-level bit-flips in .pdb files?",
        "faults": [
            # Every 10 min, cj_storage picks a random .pdb, finds its
            # physical disk block, flips one bit with dd, drops the page
            # cache. The OS sees a clean file. The job reads corrupt data.
            StorageCorrupt(
                pattern="*.pdb",
                directory=PDB_DIR,
                interval="10m",
                recursive=False,
            ),
        ],
    },

    # ── E3  Network delay ─────────────────────────────────────────────────────
    {
        "name":     "E3-net-delay",
        "label":    "Network delay only — 100ms ± 10ms",
        "question": "How sensitive is the workflow to slow file staging?",
        "faults": [
            # tc netem delays every outgoing packet by 100ms ± 10ms.
            # File transfers slow; HTCondor may hit staging timeouts.
            NetworkDelay("100ms", jitter="10ms"),
        ],
    },

    # ── E4  Packet loss ───────────────────────────────────────────────────────
    {
        "name":     "E4-net-loss",
        "label":    "Network packet loss only — 5%",
        "question": "Does HTCondor handle unreliable connections gracefully?",
        "faults": [
            # tc netem randomly drops 5% of outgoing packets.
            # TCP retransmits recover most; throughput drops ~70%.
            # HTCondor heartbeats may also be lost → worker appears offline.
            NetworkLoss("5%"),
        ],
    },

    # ── E5  Network corruption ────────────────────────────────────────────────
    {
        "name":     "E5-net-corrupt",
        "label":    "Network packet corruption only — 1%",
        "question": "Does Pegasus verify file integrity (checksums) after transfer?",
        "faults": [
            # tc netem flips random bits in 1% of packets.
            # TCP checksum catches most; rare: corrupt data reaches application.
            NetworkCorrupt("1%"),
        ],
    },

    # ── E6  Storage + delay ───────────────────────────────────────────────────
    {
        "name":     "E6-storage-delay",
        "label":    "Storage corruption + 100ms delay",
        "question": "Does slow staging give storage chaos more time to corrupt files?",
        "faults": [
            NetworkDelay("100ms", jitter="10ms"),
            StorageCorrupt("*.pdb", PDB_DIR, interval="10m", recursive=False),
        ],
    },

    # ── E7  Storage + packet loss ─────────────────────────────────────────────
    {
        "name":     "E7-storage-loss",
        "label":    "Storage corruption + 5% packet loss",
        "question": "Can you trace faults when jobs are rescheduled to other workers?",
        "faults": [
            NetworkLoss("5%"),
            StorageCorrupt("*.pdb", PDB_DIR, interval="10m", recursive=False),
        ],
    },

    # ── E8  Storage + network corruption ─────────────────────────────────────
    {
        "name":     "E8-storage-net-corrupt",
        "label":    "Storage corruption + 1% network corruption",
        "question": "Can you distinguish disk corruption from in-transit corruption?",
        "faults": [
            NetworkCorrupt("1%"),
            StorageCorrupt("*.pdb", PDB_DIR, interval="10m", recursive=False),
        ],
    },

    # ── E9  All chaos ─────────────────────────────────────────────────────────
    {
        "name":     "E9-all-chaos",
        "label":    "All chaos combined",
        "question": "What does partial workflow success look like under maximum chaos?",
        "faults": [
            # NOTE: tc netem accepts multiple options in one qdisc rule.
            # We use NetworkDelay as the primary fault because it allows
            # combined parameters.  See docs/examples.rst §2.
            NetworkDelay("100ms", jitter="10ms"),   # delay
            # Loss, corrupt, duplicate cannot share a qdisc with delay —
            # combine in one tc qdisc add call via manual parameters,
            # OR run on separate interfaces. Here we keep them separate:
            # NetworkLoss("5%"),      # ← uncomment for separate interface
            # NetworkCorrupt("1%"),
            # NetworkDuplicate("0.5%"),
            StorageCorrupt("*.pdb", PDB_DIR, interval="10m", recursive=False),
        ],
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# WORKFLOW RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def run_workflow(experiment_name: str) -> dict:
    """Submit the Pegasus workflow and wait for it to finish.

    Returns a summary dict with job counts and wall-clock time.
    In a real study replace this with your actual pegasus-run invocation.
    """
    print(f"  [workflow] Submitting {experiment_name} ...")
    t0 = time.monotonic()

    submit = make_submit()
    submit.connect()
    try:
        # ── Submit ────────────────────────────────────────────────────────
        code, stdout, stderr = submit.run(
            f"cd {WORKFLOW_DIR} && pegasus-run --output-dir results/{experiment_name}"
        )
        if code != 0:
            return {"status": "submit-failed", "stderr": stderr, "wall_s": 0}

        # Extract the run directory from pegasus-run output
        run_dir = None
        for line in stdout.splitlines():
            if "Running in directory" in line or "Submit directory" in line:
                run_dir = line.split()[-1]

        # ── Wait for completion ───────────────────────────────────────────
        poll_interval = 30   # seconds between status checks
        timeout_s     = 7200 # 2 hours hard limit
        elapsed       = 0
        while elapsed < timeout_s:
            if run_dir:
                code, out, _ = submit.run(
                    f"pegasus-status --long {run_dir} 2>/dev/null | tail -5"
                )
                if "Success" in out or "100.0%" in out:
                    break
                if "Failed" in out:
                    break
            time.sleep(poll_interval)
            elapsed += poll_interval

        # ── Collect statistics ────────────────────────────────────────────
        stats: dict = {"status": "ok", "wall_s": round(time.monotonic() - t0, 1)}
        if run_dir:
            _, stat_out, _ = submit.run(
                f"pegasus-statistics {run_dir} 2>/dev/null | grep -E 'succeeded|failed|total'"
            )
            stats["pegasus_stats"] = stat_out.strip()

    finally:
        submit.disconnect()

    return stats


# ═══════════════════════════════════════════════════════════════════════════════
# RESULT ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def compare_scores(experiment_name: str) -> dict:
    """Diff output score files against the E1 baseline to detect wrong results.

    Counts:
      - files_ok       : identical to baseline
      - files_changed  : numeric scores differ
      - files_missing  : job did not produce output
    """
    baseline_dir = RESULTS_DIR / "E1-baseline" / "scores"
    exp_dir      = RESULTS_DIR / experiment_name / "scores"

    if not baseline_dir.exists() or not exp_dir.exists():
        return {"error": "score directories not found — run collect_results() first"}

    ok = changed = missing = 0
    for score_file in baseline_dir.glob("*.sc"):
        exp_file = exp_dir / score_file.name
        if not exp_file.exists():
            missing += 1
        elif score_file.read_text() != exp_file.read_text():
            changed += 1
        else:
            ok += 1

    return {"files_ok": ok, "files_changed": changed, "files_missing": missing}


def collect_results(experiment_name: str) -> None:
    """Download score files from the submit node to the local results directory."""
    out_dir = RESULTS_DIR / experiment_name / "scores"
    out_dir.mkdir(parents=True, exist_ok=True)

    submit = make_submit()
    submit.connect()
    try:
        remote_scores = f"{WORKFLOW_DIR}/results/{experiment_name}/**/*_score.sc"
        code, stdout, _ = submit.run(f"ls {remote_scores} 2>/dev/null")
        for remote_path in stdout.splitlines():
            if remote_path.strip():
                local_path = out_dir / Path(remote_path).name
                submit.get(remote_path, str(local_path))
    finally:
        submit.disconnect()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN STUDY LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def run_study(
    experiments: list[dict],
    dry_run: bool = False,
) -> list[dict]:
    """Run experiments sequentially, collecting chaos logs and workflow results.

    Each experiment:
      1. Starts chaos on the worker node
      2. Submits and waits for the Pegasus workflow
      3. Stops and reverts chaos
      4. Downloads score files
      5. Compares scores against the E1 baseline
      6. Saves the chaos session log to disk

    Parameters
    ----------
    experiments :
        Subset of the EXPERIMENTS list to run.
    dry_run :
        If True, print the plan but do not connect to any target.

    Returns
    -------
    list[dict]
        One result dict per experiment.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_results = []

    print(f"\n{'='*70}")
    print(f"  Chaos-Jungle Paper Study  —  {len(experiments)} experiment(s)")
    print(f"  Worker  : {WORKER_USER}@{WORKER_HOST}")
    print(f"  Duration: {EXPERIMENT_DURATION} per experiment")
    print(f"  Results : {RESULTS_DIR.resolve()}")
    print(f"{'='*70}\n")

    for i, exp in enumerate(experiments, 1):
        name   = exp["name"]
        faults = exp["faults"]
        print(f"[{i}/{len(experiments)}]  {name}")
        print(f"  {exp['label']}")
        print(f"  Question: {exp['question']}")
        if faults:
            for f in faults:
                print(f"  Fault: {f.__class__.__name__}({f._parameters()})")
        else:
            print("  Fault: none (baseline)")

        if dry_run:
            print("  [dry-run] skipping\n")
            continue

        result = {"experiment": name, "chaos_error": None, "workflow": {}, "scores": {}}

        # ── Build runner ──────────────────────────────────────────────────
        from chaos_jungle import Scenario, ChaosRunner
        scenario = Scenario(name, faults)
        worker   = make_worker()
        runner   = ChaosRunner(
            scenario,
            target=worker,
            auto_install=True,    # install tc/filefrag if missing
            conflict="raise",     # guardrails on
        )

        # ── Inject chaos ──────────────────────────────────────────────────
        try:
            runner.start()
            print(f"  [chaos] ON  (session {runner._session_id})")
        except Exception as exc:
            print(f"  [chaos] FAILED to start: {exc}")
            result["chaos_error"] = str(exc)
            all_results.append(result)
            continue

        # ── Run workflow under chaos ───────────────────────────────────────
        try:
            result["workflow"] = run_workflow(name)
            print(f"  [workflow] done  status={result['workflow'].get('status')}  "
                  f"wall={result['workflow'].get('wall_s')}s")
        except Exception as exc:
            print(f"  [workflow] ERROR: {exc}")
            result["workflow"] = {"status": "error", "error": str(exc)}
        finally:
            # ── Always revert chaos ───────────────────────────────────────
            try:
                runner.stop()
                print("  [chaos] OFF — reverted")
            except Exception as exc:
                print(f"  [chaos] ERROR during stop: {exc}")
                result["chaos_error"] = str(exc)

        # ── Save chaos session log ────────────────────────────────────────
        session_log_path = RESULTS_DIR / name / "chaos-session.json"
        session_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(session_log_path, "w") as fh:
            fh.write(runner.export("json"))
        print(f"  [log] saved → {session_log_path}")

        # ── Collect + compare scores ──────────────────────────────────────
        if result["workflow"].get("status") == "ok":
            try:
                collect_results(name)
                if name != "E1-baseline":
                    result["scores"] = compare_scores(name)
                    sc = result["scores"]
                    print(f"  [scores] ok={sc.get('files_ok')}  "
                          f"changed={sc.get('files_changed')}  "
                          f"missing={sc.get('files_missing')}")
            except Exception as exc:
                result["scores"] = {"error": str(exc)}

        all_results.append(result)
        print()

    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ═══════════════════════════════════════════════════════════════════════════════

def print_summary(results: list[dict]) -> None:
    """Print a comparison table across all experiments."""
    print(f"\n{'='*90}")
    print(f"  {'EXPERIMENT':<28}  {'WORKFLOW':^12}  {'OK':>5}  {'CHANGED':>7}  {'MISSING':>7}  NOTES")
    print(f"  {'-'*88}")

    for r in results:
        name     = r["experiment"]
        wf       = r["workflow"]
        sc       = r.get("scores", {})
        status   = wf.get("status", "-")
        ok       = sc.get("files_ok",      "-")
        changed  = sc.get("files_changed", "-")
        missing  = sc.get("files_missing", "-")
        notes    = r.get("chaos_error") or wf.get("stderr", "")[:30] or ""
        print(f"  {name:<28}  {status:^12}  {str(ok):>5}  {str(changed):>7}  {str(missing):>7}  {notes}")

    print(f"{'='*90}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the chaos-jungle paper study (E1–E9)"
    )
    parser.add_argument(
        "--experiment", "-e",
        help="Run only this experiment (e.g. E3-net-delay). Default: all.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print plan without connecting to any target.",
    )
    args = parser.parse_args()

    experiments = EXPERIMENTS
    if args.experiment:
        experiments = [e for e in EXPERIMENTS if e["name"] == args.experiment]
        if not experiments:
            print(f"ERROR: unknown experiment {args.experiment!r}")
            print(f"Valid names: {[e['name'] for e in EXPERIMENTS]}")
            sys.exit(1)

    results = run_study(experiments, dry_run=args.dry_run)

    if not args.dry_run and results:
        print_summary(results)

        # Save full results as JSON
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = RESULTS_DIR / f"study-report-{ts}.json"
        with open(report_path, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"Full report saved → {report_path}")


if __name__ == "__main__":
    main()
