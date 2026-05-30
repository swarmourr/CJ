#!/usr/bin/env python3
"""
chaos-jungle quickstart example
================================
Runs a NetworkDelay fault on the local machine, measures the real effect
with ping, records results, prints a summary, and persists everything to
~/.chaos-jungle/chaos_jungle.db for the dashboard.

Requirements on this machine:
  sudo apt-get install -y iproute2   # for tc (network faults)

Usage:
  sudo python3 examples/quickstart.py
  chaos-jungle dashboard              # then open http://127.0.0.1:8050
"""

import subprocess
import time

from chaos_jungle import Scenario, ChaosRunner
from chaos_jungle.faults import NetworkDelay
from chaos_jungle.targets import LocalTarget

# ── 1. Target and scenario ────────────────────────────────────────────────

target = LocalTarget()

scenario = Scenario(
    name="quickstart-network-delay",
    faults=[
        NetworkDelay(delay="100ms", jitter="10ms"),
    ],
)

# ── 2. Preflight — verify tc is available ─────────────────────────────────

print("\n[preflight] Checking dependencies...")
try:
    NetworkDelay("100ms").preflight(target, auto_install=False)
    print("[preflight] OK — tc is available")
except Exception as e:
    print(f"[preflight] MISSING: {e}")
    print("\nFix: sudo apt-get install -y iproute2  then re-run with sudo")
    raise SystemExit(1)

# ── 3. Measure baseline latency (no chaos) ───────────────────────────────

def ping_latency(host="127.0.0.1", count=3):
    """Return average ping RTT in ms, or None on failure."""
    result = subprocess.run(
        ["ping", "-c", str(count), "-W", "2", host],
        capture_output=True, text=True,
    )
    times = []
    for line in result.stdout.splitlines():
        if "time=" in line:
            try:
                times.append(float(line.split("time=")[1].split()[0]))
            except (IndexError, ValueError):
                pass
    return round(sum(times) / len(times), 2) if times else None

print("\n[baseline] Measuring latency without chaos...")
baseline_ms = ping_latency()
print(f"[baseline] Avg RTT = {baseline_ms} ms")

# ── 4. Start chaos ────────────────────────────────────────────────────────

runner = ChaosRunner(
    scenario,
    target=target,
    auto_install=False,
    conflict="warn",
)

print("\n[chaos] Starting fault injection...")
runner.start()

# ── 5. Workload: ping under chaos ─────────────────────────────────────────

print("[chaos] Measuring latency WITH 100ms delay injected...")
chaos_latencies = []
for i in range(5):
    lat = ping_latency(count=1)
    if lat is not None:
        chaos_latencies.append(lat)
        print(f"  ping {i+1}: {lat:.1f} ms")
    time.sleep(0.5)

avg_chaos_ms = round(sum(chaos_latencies) / len(chaos_latencies), 2) if chaos_latencies else 0
added_delay  = round(avg_chaos_ms - (baseline_ms or 0), 2)
effective    = avg_chaos_ms > (baseline_ms or 0) + 50   # >50ms added = fault is working

# ── 6. Record results ─────────────────────────────────────────────────────

runner.record_result({
    "baseline_latency_ms":   baseline_ms,
    "chaos_latency_ms":      avg_chaos_ms,
    "added_delay_ms":        added_delay,
    "pings_sent":            5,
    "pings_received":        len(chaos_latencies),
    "fault_effective":       int(effective),
    "expected_delay_ms":     100,
})

# ── 7. Stop chaos ─────────────────────────────────────────────────────────

runner.stop()

# ── 8. Summary ────────────────────────────────────────────────────────────

s = runner.summary()

print("\n" + "=" * 55)
print(f"  Scenario   : {s['name']}")
print(f"  Session ID : {s['session_id']}")
print(f"  Status     : {s['status']}")
print(f"  Duration   : {s['duration_s']}s")
print("-" * 55)
print(f"  Baseline latency : {baseline_ms} ms")
print(f"  Chaos latency    : {avg_chaos_ms} ms  (target: ~110ms)")
print(f"  Added delay      : {added_delay} ms")
print(f"  Fault effective  : {'YES ✓' if effective else 'NO ✗ (check sudo/tc)'}")
print("-" * 55)
print(f"  Errors     : {s['errors'] or 'none'}")
print("=" * 55)
print(f"\n  DB : ~/.chaos-jungle/chaos_jungle.db")
print(f"  Run: chaos-jungle dashboard")
print(f"       chaos-jungle list")
print(f"       chaos-jungle export --session {s['session_id']} --format json")
