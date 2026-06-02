"""
Scenario: Network delay — inject latency using tc netem.
Tests whether wget retries or times out under high latency.

Expected result:
  Low delay  (50ms)  : transfers complete, no retries
  High delay (2000ms): wget may timeout or retry
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from config import SRC_NODES, DEST_NODES, SITE_DIR, TEMPLATE_DIR
from utils import make_target, result_dir, print_banner
from chaos_jungle import ChaosRunner, Scenario
from chaos_jungle.faults import NetworkDelay


def run(delay: str = "500ms"):
    print_banner(f"Network Delay — {delay}")
    rdir    = result_dir("network_delay")
    src_ip  = SRC_NODES[0]
    dest_ip = DEST_NODES[0]
    run_dir = f"{SITE_DIR}/run_delay"

    # 1. create run data
    with make_target(src_ip) as src:
        src.run(f"rm -rf {run_dir} && mkdir -p {run_dir}")
        src.run(f"cp -r {TEMPLATE_DIR} {run_dir}/")
        print(f"  data ready at {run_dir}")

    # 2. inject delay on src outgoing traffic
    fault  = NetworkDelay(delay, jitter="10ms")
    target = make_target(src_ip)
    runner = ChaosRunner(Scenario("network-delay", faults=[fault]), target)

    runner.start()
    print(f"  delay {delay} active on {src_ip}")

    # 3. transfer + measure
    import time
    with make_target(dest_ip) as dest:
        wget_log = os.path.join(rdir, "delay_wget.log")
        diff_log = os.path.join(rdir, "delay_diff.log")

        t0 = time.time()
        _, wget_out, _ = dest.run(
            f"wget -q -P /tmp/delay_dl -r -m --no-parent -R 'index.html*' "
            f"--timeout=30 --tries=3 "
            f"http://{src_ip}/run_delay/ 2>&1"
        )
        elapsed = time.time() - t0

        _, diff_out, _ = dest.run(
            f"diff -qr {TEMPLATE_DIR} "
            f"/tmp/delay_dl/{src_ip}/run_delay/$(basename {TEMPLATE_DIR}) "
            f"2>&1"
        )

        with open(wget_log, "w") as f: f.write(wget_out)
        with open(diff_log, "w") as f: f.write(diff_out)

        retries  = wget_out.count("Retrying")
        failures = [l for l in diff_out.splitlines() if l.startswith("Files")]

        print(f"  transfer time : {elapsed:.1f}s")
        print(f"  wget retries  : {retries}")
        print(f"  diff failures : {len(failures)} (should be 0 — delay doesn't corrupt)")

        if not failures:
            print(f"  PASS — data integrity maintained under {delay} delay")
        else:
            print(f"  FAIL — data corrupted (unexpected for delay fault)")

        dest.run("rm -rf /tmp/delay_dl")

    # 4. stop
    runner.stop()

    print(f"\nResults: {rdir}")


if __name__ == "__main__":
    delay = sys.argv[1] if len(sys.argv) > 1 else "500ms"
    run(delay)
