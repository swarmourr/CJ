"""
Scenario: Silent network corruption — BPF flips bytes in flight,
TCP checksum is recalculated so the receiver never sees an error.

Expected result:
  RETRIES  = 0  (TCP never retries — checksum looks valid)
  FAILURE  = 1  (diff catches corrupted bytes)

Requirements on target:
  - Linux kernel >= 4.15
  - apt-get install bpfcc-tools python3-bpfcc
  - ~/chaos-jungle repo cloned (contains flow_modify.c)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from config import SRC_NODES, DEST_NODES, SITE_DIR, TEMPLATE_DIR
from utils import make_target, result_dir, print_banner
from chaos_jungle import ChaosRunner, Scenario
from chaos_jungle.faults.bpf import SilentNetworkCorrupt


def run(rate: int = 100):
    print_banner(f"Silent Network Corruption — 1 in {rate} packets")
    rdir    = result_dir("network_silent")
    src_ip  = SRC_NODES[0]
    dest_ip = DEST_NODES[0]
    run_dir = f"{SITE_DIR}/run_network"

    # 1. create clean run data on src
    with make_target(src_ip) as src:
        src.run(f"rm -rf {run_dir} && mkdir -p {run_dir}")
        src.run(f"cp -r {TEMPLATE_DIR} {run_dir}/")
        print(f"  data ready at {run_dir}")

    # 2. inject BPF network fault on src (corrupts outgoing packets)
    fault  = SilentNetworkCorrupt(rate=rate, hook="tc")
    target = make_target(src_ip)
    runner = ChaosRunner(Scenario("network-silent", faults=[fault]), target)

    runner.start()
    print(f"  BPF hook active on {src_ip} — 1/{rate} packets corrupted")

    # 3. wget + diff while fault is active
    with make_target(dest_ip) as dest:
        wget_log = os.path.join(rdir, "network_wget.log")
        diff_log = os.path.join(rdir, "network_diff.log")

        _, wget_out, _ = dest.run(
            f"wget -q -P /tmp/network_dl -r -m --no-parent -R 'index.html*' "
            f"http://{src_ip}/run_network/ 2>&1"
        )

        _, diff_out, _ = dest.run(
            f"diff -qr {TEMPLATE_DIR} "
            f"/tmp/network_dl/{src_ip}/run_network/$(basename {TEMPLATE_DIR}) "
            f"2>&1"
        )

        with open(wget_log, "w") as f: f.write(wget_out)
        with open(diff_log, "w") as f: f.write(diff_out)

        retries  = wget_out.count("Retrying")
        failures = [l for l in diff_out.splitlines() if l.startswith("Files")]

        print(f"  wget retries : {retries}  (should be 0 — TCP checksum was valid)")
        print(f"  diff failures: {len(failures)} file(s) corrupted")

        if failures and retries == 0:
            print(f"  PASS — silent corruption confirmed: diff detects, wget does not")
        elif failures and retries > 0:
            print(f"  PARTIAL — corruption detected but TCP also noticed (not fully silent)")
        else:
            print(f"  WARN — no corruption detected (rate may be too low for file sizes)")

        dest.run("rm -rf /tmp/network_dl")

    # 4. stop BPF hook
    runner.stop()
    print(f"  BPF hook detached")

    print(f"\nResults: {rdir}")


if __name__ == "__main__":
    rate = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    run(rate)
