"""
Run on a Linux machine with sudo access:

    python examples/demo_output.py

Expected output
---------------
[chaos-jungle] Injecting NetworkDelay({'delay': '100ms', 'jitter': '10ms', 'iface': None})
[chaos-jungle] Chaos ON  — scenario 'demo'  (session id: 1)

  workload tick 1/5
  workload tick 2/5
  workload tick 3/5
  workload tick 4/5
  workload tick 5/5

[chaos-jungle] Reverted NetworkDelay
[chaos-jungle] Chaos OFF — session 1 reverted.

--- summary ---
name       : demo
session_id : 1
status     : reverted
duration_s : 5.1s
faults     : [{'kind': 'NetworkDelay', 'parameters': {'delay': '100ms', ...}}]
errors     : none
"""

import time
import json
from chaos_jungle import Scenario, ChaosRunner
from chaos_jungle.faults import NetworkDelay
from chaos_jungle.targets import LocalTarget

# ── build ──────────────────────────────────────────────────────────
scenario = Scenario("demo", faults=[NetworkDelay("100ms", jitter="10ms")])
runner   = ChaosRunner(scenario, LocalTarget(), conflict="warn")

# ── start chaos ────────────────────────────────────────────────────
runner.start()

# ── workload ───────────────────────────────────────────────────────
for i in range(1, 6):
    time.sleep(1)
    print(f"  workload tick {i}/5")

# ── stop chaos ─────────────────────────────────────────────────────
runner.stop()

# ── summary ────────────────────────────────────────────────────────
print("\n--- summary ---")
s = runner.summary()
print(f"name       : {s['name']}")
print(f"session_id : {s['session_id']}")
print(f"status     : {s['status']}")
print(f"duration_s : {s['duration_s']}s")
print(f"faults     : {s['faults']}")
print(f"errors     : {s['errors'] or 'none'}")

# ── full export ────────────────────────────────────────────────────
print("\n--- full export ---")
print(runner.export("json"))
