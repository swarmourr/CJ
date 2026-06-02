"""
Run all scenarios in sequence and print a summary.

Usage:
    python run_all.py
    python run_all.py baseline storage network_delay
"""

import sys
import os
import traceback
sys.path.insert(0, os.path.dirname(__file__))


SCENARIOS = {
    "baseline":       ("baseline",       {}),
    "storage":        ("storage_corrupt", {"prob": 1.0}),
    "network_silent": ("network_silent",  {"rate": 100}),
    "network_delay":  ("network_delay",   {"delay": "500ms"}),
}


def main():
    to_run = sys.argv[1:] or list(SCENARIOS.keys())
    results = {}

    for name in to_run:
        if name not in SCENARIOS:
            print(f"[skip] unknown scenario: {name}")
            continue
        module_name, kwargs = SCENARIOS[name]
        try:
            mod = __import__(module_name)
            mod.run(**kwargs)
            results[name] = "OK"
        except Exception as e:
            print(f"\n[ERROR] {name}: {e}")
            traceback.print_exc()
            results[name] = f"FAIL: {e}"

    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    for name, status in results.items():
        icon = "✓" if status == "OK" else "✗"
        print(f"  {icon}  {name:<20} {status}")
    print()


if __name__ == "__main__":
    main()
