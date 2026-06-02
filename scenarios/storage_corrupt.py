"""
Scenario: Storage corruption — inject file corruption on src node,
transfer files to dest, check if diff detects it.

Expected result:
  RETRIES  = 0  (wget never retries — HTTP 200 always)
  FAILURE  = 1  (diff catches corrupted bytes)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from config import SRC_NODES, DEST_NODES, SITE_DIR, TEMPLATE_DIR, CJ_DIR
from utils import make_target, result_dir, print_banner
from chaos_jungle import ChaosRunner, Scenario
from chaos_jungle.faults import StorageCorrupt


def run(prob: float = 1.0):
    print_banner(f"Storage Corruption — prob={prob}")
    rdir    = result_dir("storage_corrupt")
    src_ip  = SRC_NODES[0]
    dest_ip = DEST_NODES[0]
    run_dir = f"{SITE_DIR}/run_storage"

    # 1. create run data on src
    with make_target(src_ip) as src:
        src.run(f"rm -rf {run_dir} && mkdir -p {run_dir}")
        src.run(f"cp -r {TEMPLATE_DIR} {run_dir}/")
        print(f"  data ready at {run_dir}")

    # 2. inject storage fault + transfer + stop
    fault  = StorageCorrupt(pattern="*", directory=run_dir, interval="30s", recursive=True)
    target = make_target(src_ip)
    runner = ChaosRunner(Scenario("storage-corrupt", faults=[fault]), target)

    runner.start()
    print(f"  storage corruption active on {src_ip}")

    # 3. wget + diff on dest
    with make_target(dest_ip) as dest:
        wget_log = os.path.join(rdir, "storage_wget.log")
        diff_log = os.path.join(rdir, "storage_diff.log")

        _, wget_out, _ = dest.run(
            f"wget -q -P /tmp/storage_dl -r -m --no-parent -R 'index.html*' "
            f"http://{src_ip}/run_storage/ 2>&1"
        )

        _, diff_out, _ = dest.run(
            f"diff -qr {TEMPLATE_DIR} "
            f"/tmp/storage_dl/{src_ip}/run_storage/$(basename {TEMPLATE_DIR}) "
            f"2>&1"
        )

        with open(wget_log, "w") as f: f.write(wget_out)
        with open(diff_log, "w") as f: f.write(diff_out)

        failures = [l for l in diff_out.splitlines() if l.startswith("Files")]
        print(f"  wget  : {'OK' if not wget_out.strip() else 'errors'}")
        print(f"  diff  : {len(failures)} file(s) corrupted detected")

        if failures:
            print(f"  PASS — corruption detected by diff")
        else:
            print(f"  WARN — no corruption detected (prob may be too low or timing)")

        dest.run("rm -rf /tmp/storage_dl")

    # 4. stop + revert
    runner.stop()

    # 5. collect cj.log
    with make_target(src_ip) as src:
        rc, cj_log, _ = src.run("cat /var/log/cj.log 2>/dev/null")
        if rc == 0 and cj_log.strip():
            with open(os.path.join(rdir, "cj.log"), "w") as f:
                f.write(cj_log)
            corrupted = cj_log.count("CORRUPT record")
            print(f"  cj.log: {corrupted} bit-flip records")
        src.run("rm -f /var/log/cj.log")

    print(f"\nResults: {rdir}")


if __name__ == "__main__":
    prob = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    run(prob)
