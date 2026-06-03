#!/usr/bin/env python3
"""
Scenario 3 — Fault Composition (multiple faults, compounding effect)
=====================================================================
Runs the same workload three times:
  Run A — StorageCorrupt alone
  Run B — NetworkDelay alone
  Run C — StorageCorrupt + NetworkDelay together

Shows that simultaneous faults compound: the combined impact is greater
than either fault alone.

Workload : copy .pdb files, verify MD5, record transfer time + errors
Faults   : StorageCorrupt(interval=5s) + NetworkDelay("800ms")
Measures : duration_s, errors, integrity_rate per run

Usage:
    sudo python3 examples/fault_composition.py
"""

import hashlib
import os
import shutil
import tempfile
import time

from chaos_jungle import Scenario, ChaosRunner, MeasurementResult
from chaos_jungle.faults import StorageCorrupt, NetworkDelay
from chaos_jungle.targets import LocalTarget

# ── Setup ─────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTDATA   = os.path.join(SCRIPT_DIR, "..", "cj-scenarios", "testdata",
                          "20190425T121649-0700", "00", "00")

SRC_DIR  = tempfile.mkdtemp(prefix="cj_comp_src_")
DEST_DIR = tempfile.mkdtemp(prefix="cj_comp_dst_")


def _setup_source():
    if os.path.isdir(TESTDATA):
        for f in os.listdir(TESTDATA):
            if f.endswith(".pdb"):
                shutil.copy(os.path.join(TESTDATA, f), SRC_DIR)
    if not os.listdir(SRC_DIR):
        for i in range(10):
            p = os.path.join(SRC_DIR, f"protein_{i:03d}.pdb")
            with open(p, "wb") as fh:
                fh.write(os.urandom(4096))


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def transfer_and_verify() -> dict:
    for f in os.listdir(DEST_DIR):
        os.remove(os.path.join(DEST_DIR, f))
    files = [f for f in os.listdir(SRC_DIR) if f.endswith(".pdb")]
    errors = 0
    t0 = time.time()
    for fname in files:
        src = os.path.join(SRC_DIR, fname)
        dst = os.path.join(DEST_DIR, fname)
        shutil.copy2(src, dst)
        if _md5(src) != _md5(dst):
            errors += 1
    duration = round(time.time() - t0, 4)
    total = len(files)
    return {
        "duration_s":     duration,
        "errors":         errors,
        "integrity_rate": round((total - errors) / total, 4) if total else 1.0,
    }


# ── Run helper ────────────────────────────────────────────────────────────

def run_scenario(label: str, faults: list, n: int = 3) -> MeasurementResult:
    print(f"\n  [{label}] faults: {[f.__class__.__name__ for f in faults]}")
    runner = ChaosRunner(
        Scenario(label, faults=faults),
        target=LocalTarget(),
        auto_install=True,
        conflict="force",
    )
    return runner.measure(transfer_and_verify, n_baseline=n, n_fault=n)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    _setup_source()
    print(f"\n  source : {SRC_DIR}  ({len(os.listdir(SRC_DIR))} files)")

    result_a = run_scenario("storage-only", faults=[
        StorageCorrupt(directory=SRC_DIR, pattern="*.pdb", interval="5s", recursive=False),
    ])

    result_b = run_scenario("network-only", faults=[
        NetworkDelay(delay="800ms", jitter="20ms"),
    ])

    result_c = run_scenario("storage-and-network", faults=[
        StorageCorrupt(directory=SRC_DIR, pattern="*.pdb", interval="5s", recursive=False),
        NetworkDelay(delay="800ms", jitter="20ms"),
    ])

    # ── Report ────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  FAULT COMPOSITION RESULT")
    print("=" * 70)
    print(f"  {'Scenario':<25} {'errors Δ':>10}  {'integrity_rate Δ':>18}  {'duration Δ':>12}")
    print("-" * 70)
    for label, r in [("storage only", result_a),
                     ("network only", result_b),
                     ("storage + network", result_c)]:
        e  = r.delta.get("errors", 0)
        ir = r.delta.get("integrity_rate", 0)
        d  = r.delta.get("duration_s", 0)
        print(f"  {label:<25} {e:>+10.2f}  {ir:>+18.4f}  {d:>+12.4f}s")
    print("=" * 70)

    combined_worse_integrity = (
        result_c.delta.get("errors", 0) >=
        result_a.delta.get("errors", 0) + result_b.delta.get("errors", 0)
    )
    print(f"\n  Combined fault is additive or worse : {combined_worse_integrity}")

    shutil.rmtree(SRC_DIR, ignore_errors=True)
    shutil.rmtree(DEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
