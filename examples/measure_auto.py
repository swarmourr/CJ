#!/usr/bin/env python3
"""
Scenario 1 — Automatic Baseline vs Fault Measurement
=====================================================
Uses runner.measure() to automatically run the workload under baseline
and fault conditions, compute delta, and report.

Workload : copy .pdb protein files, verify MD5 integrity
Fault    : StorageCorrupt — flips bytes every 5s
Measures : duration_s, errors, integrity_rate

Usage:
    sudo python3 examples/measure_auto.py
"""

import hashlib
import os
import shutil
import tempfile
import time

from chaos_jungle import Scenario, ChaosRunner, MeasurementResult
from chaos_jungle.faults import StorageCorrupt
from chaos_jungle.targets import LocalTarget

# ── Setup ─────────────────────────────────────────────────────────────────

# Use bundled testdata if available, else create synthetic files
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTDATA   = os.path.join(SCRIPT_DIR, "..", "cj-scenarios", "testdata",
                          "20190425T121649-0700", "00", "00")

SRC_DIR  = tempfile.mkdtemp(prefix="cj_src_")
DEST_DIR = tempfile.mkdtemp(prefix="cj_dst_")


def _setup_source():
    """Populate SRC_DIR with .pdb files (real or synthetic)."""
    if os.path.isdir(TESTDATA):
        for f in os.listdir(TESTDATA):
            if f.endswith(".pdb"):
                shutil.copy(os.path.join(TESTDATA, f), SRC_DIR)
    if not os.listdir(SRC_DIR):
        # synthetic fallback — 10 small binary files
        for i in range(10):
            path = os.path.join(SRC_DIR, f"protein_{i:03d}.pdb")
            with open(path, "wb") as fh:
                fh.write(os.urandom(4096))
    print(f"  source : {SRC_DIR}  ({len(os.listdir(SRC_DIR))} files)")
    print(f"  dest   : {DEST_DIR}")


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


# ── Workload ──────────────────────────────────────────────────────────────

def transfer_and_verify() -> dict:
    """Copy all files from SRC_DIR to DEST_DIR, verify MD5, return metrics."""
    # clear destination
    for f in os.listdir(DEST_DIR):
        os.remove(os.path.join(DEST_DIR, f))

    src_files = [f for f in os.listdir(SRC_DIR) if f.endswith(".pdb")]
    errors = 0
    t0 = time.time()

    for fname in src_files:
        src = os.path.join(SRC_DIR, fname)
        dst = os.path.join(DEST_DIR, fname)
        shutil.copy2(src, dst)
        if _md5(src) != _md5(dst):
            errors += 1

    duration = round(time.time() - t0, 4)
    total = len(src_files)
    integrity_rate = round((total - errors) / total, 4) if total else 1.0

    return {
        "duration_s":     duration,
        "files_total":    total,
        "errors":         errors,
        "integrity_rate": integrity_rate,
    }


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    _setup_source()

    runner = ChaosRunner(
        Scenario("storage-measure", faults=[
            StorageCorrupt(
                directory=SRC_DIR,
                pattern="*.pdb",
                interval="5s",
                recursive=False,
            )
        ]),
        target=LocalTarget(),
        auto_install=True,
        conflict="force",
    )

    print("\n  Running measure() — 3 baseline trials, 3 fault trials ...\n")
    result: MeasurementResult = runner.measure(
        transfer_and_verify,
        n_baseline=3,
        n_fault=3,
    )

    print("\n" + "=" * 60)
    print(result.summary())
    print("=" * 60)
    print(f"\n  integrity degraded : {not result.passed('integrity_rate', threshold=0.0)}")
    print(f"  errors introduced  : {result.delta.get('errors', 0):+.2f} per run")
    print(f"  session id         : {result.session_id}")
    print("\n  Run: chaos-jungle dashboard")

    # cleanup
    shutil.rmtree(SRC_DIR, ignore_errors=True)
    shutil.rmtree(DEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
