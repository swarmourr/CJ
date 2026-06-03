#!/usr/bin/env python3
"""
Scenario 2 — Fault Scheduling (inject mid-workload)
====================================================
Starts a long-running workload, then injects a fault 15 seconds in
without modifying the workload code at all.

Workload : writes checkpointed results every 2 seconds for 40 seconds
Fault    : NetworkDelay("1500ms") injected at start_after=15s, duration=15s
Measures : successful writes before/during/after fault window

Usage:
    sudo python3 examples/fault_scheduling.py
"""

import os
import tempfile
import threading
import time

from chaos_jungle import Scenario, ChaosRunner
from chaos_jungle.faults import NetworkDelay
from chaos_jungle.targets import LocalTarget

# shared state written by workload, read by reporter
_results: list[dict] = []
_lock = threading.Lock()


# ── Workload ──────────────────────────────────────────────────────────────

def long_running_job(workdir: str, total_s: int = 40, interval_s: float = 2.0):
    """Simulate a scientific job that writes checkpoint files periodically."""
    step = 0
    t_start = time.time()
    print(f"  [job] started — writing checkpoints every {interval_s}s for {total_s}s")

    while time.time() - t_start < total_s:
        step += 1
        t = round(time.time() - t_start, 1)
        path = os.path.join(workdir, f"checkpoint_{step:03d}.dat")
        try:
            with open(path, "wb") as fh:
                fh.write(os.urandom(1024))
            with _lock:
                _results.append({"step": step, "t": t, "ok": True})
            print(f"  [job] step {step:3d}  t={t:5.1f}s  OK")
        except Exception as e:
            with _lock:
                _results.append({"step": step, "t": t, "ok": False, "err": str(e)})
            print(f"  [job] step {step:3d}  t={t:5.1f}s  FAIL — {e}")
        time.sleep(interval_s)

    print(f"  [job] finished — {step} steps completed")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    workdir = tempfile.mkdtemp(prefix="cj_sched_")
    print(f"\n  workdir : {workdir}")

    runner = ChaosRunner(
        Scenario("scheduled-delay", faults=[
            NetworkDelay(delay="1500ms", jitter="50ms"),
        ]),
        target=LocalTarget(),
        auto_install=True,
        conflict="force",
    )

    # Schedule fault: inject after 15s, auto-clear after 15s more (at T=30s)
    print("\n  Scheduling fault: NetworkDelay(1500ms) at T=15s for 15s\n")
    runner.start(start_after=15, duration=15)

    # Run the workload in the foreground — fault will hit mid-run
    long_running_job(workdir, total_s=40, interval_s=2.0)

    # ── Analyse results ───────────────────────────────────────────────────
    with _lock:
        all_steps = list(_results)

    before  = [r for r in all_steps if r["t"] < 15]
    during  = [r for r in all_steps if 15 <= r["t"] < 30]
    after   = [r for r in all_steps if r["t"] >= 30]

    ok_before = sum(1 for r in before if r["ok"])
    ok_during = sum(1 for r in during if r["ok"])
    ok_after  = sum(1 for r in after  if r["ok"])

    print("\n" + "=" * 55)
    print("  FAULT SCHEDULING RESULT")
    print("=" * 55)
    print(f"  T=0–15s   (no fault)  : {ok_before}/{len(before)} steps OK")
    print(f"  T=15–30s  (fault ON)  : {ok_during}/{len(during)} steps OK")
    print(f"  T=30–40s  (fault OFF) : {ok_after}/{len(after)}  steps OK")
    print("=" * 55)
    print(f"  Fault window detectable : {ok_during < ok_before}")
    print(f"  Recovery after fault    : {ok_after == len(after)}")

    if runner._session_id:
        runner.record_result({
            "steps_before":  len(before),
            "steps_during":  len(during),
            "steps_after":   len(after),
            "ok_before":     ok_before,
            "ok_during":     ok_during,
            "ok_after":      ok_after,
        })

    import shutil
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
