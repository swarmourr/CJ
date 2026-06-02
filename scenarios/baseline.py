"""
Scenario: Baseline — no fault injected.
Run this first to get a clean reference transfer result.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from config import SRC_NODES, DEST_NODES, SITE_DIR, TEMPLATE_DIR
from utils import make_target, result_dir, print_banner


def run():
    print_banner("Baseline — no fault")
    rdir = result_dir("baseline")

    src_ip  = SRC_NODES[0]
    dest_ip = DEST_NODES[0]

    # create run data on src
    with make_target(src_ip) as src:
        run_dir = f"{SITE_DIR}/run_baseline"
        src.run(f"rm -rf {run_dir} && mkdir -p {run_dir}")
        src.run(f"cp -r {TEMPLATE_DIR} {run_dir}/")
        print(f"  data ready at {run_dir}")

    # wget + diff on dest
    with make_target(dest_ip) as dest:
        wget_log  = os.path.join(rdir, "baseline_wget.log")
        diff_log  = os.path.join(rdir, "baseline_diff.log")

        dest.run(
            f"wget -q -P /tmp/baseline_dl -r -m --no-parent -R 'index.html*' "
            f"http://{src_ip}/run_baseline/ 2>&1"
        )

        rc, out, _ = dest.run(
            f"diff -qr {TEMPLATE_DIR} "
            f"/tmp/baseline_dl/{src_ip}/run_baseline/$(basename {TEMPLATE_DIR}) "
            f"2>&1"
        )

        with open(diff_log, "w") as f:
            f.write(out)

        if out.strip():
            print(f"  FAIL — diff found differences (unexpected in baseline):\n{out}")
        else:
            print(f"  PASS — files identical")

        # cleanup
        dest.run("rm -rf /tmp/baseline_dl")

    print(f"\nResults: {rdir}")


if __name__ == "__main__":
    run()
