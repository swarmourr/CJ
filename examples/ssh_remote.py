#!/usr/bin/env python3
"""
Scenario 6 — Remote Fault Injection via SSH
============================================
Runs the exact same file-transfer workload as measure_auto.py but
targets a remote node over SSH instead of the local machine.

Only ONE line changes compared to measure_auto.py:
    target = SSHTarget(...)   instead of   target = LocalTarget()

Everything else — faults, workload, measure(), MeasurementResult — is
identical. The library is target-agnostic.

Requirements on the remote node:
  - Python 3.8+
  - pip install chaos-jungle
  - sudo access (for StorageCorrupt crontab)

Usage:
    python3 examples/ssh_remote.py --host worker1 --user ubuntu --password secret
    python3 examples/ssh_remote.py --host worker1 --user ubuntu --key ~/.ssh/id_rsa
"""

import argparse
import hashlib
import os
import shutil
import tempfile
import time

from chaos_jungle import Scenario, ChaosRunner, MeasurementResult
from chaos_jungle.faults import StorageCorrupt, NetworkDelay
from chaos_jungle.targets import SSHTarget, LocalTarget

# ── CLI ───────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Remote chaos via SSH")
    p.add_argument("--host",     default="",  help="SSH hostname or IP")
    p.add_argument("--user",     default="",  help="SSH username")
    p.add_argument("--password", default="",  help="SSH password")
    p.add_argument("--key",      default="",  help="Path to SSH private key")
    p.add_argument("--port",     type=int, default=22, help="SSH port")
    return p.parse_args()


# ── Workload (runs locally, mirrors what would run on remote) ─────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTDATA   = os.path.join(SCRIPT_DIR, "..", "cj-scenarios", "testdata",
                          "20190425T121649-0700", "00", "00")

SRC_DIR  = tempfile.mkdtemp(prefix="cj_ssh_src_")
DEST_DIR = tempfile.mkdtemp(prefix="cj_ssh_dst_")


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
    total = len(files)
    return {
        "duration_s":     round(time.time() - t0, 4),
        "errors":         errors,
        "integrity_rate": round((total - errors) / total, 4) if total else 1.0,
    }


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    _setup_source()

    # ── Target selection — the ONLY difference from measure_auto.py ───────
    if args.host:
        print(f"\n  Target : SSH → {args.user}@{args.host}:{args.port}")
        target = SSHTarget(
            host=args.host,
            user=args.user or None,
            password=args.password or None,
            key_path=args.key or None,
            port=args.port,
        )
    else:
        print("\n  No --host given — falling back to LocalTarget")
        print("  Usage: python3 ssh_remote.py --host <ip> --user <user> --password <pw>")
        target = LocalTarget()

    # ── Everything below is IDENTICAL to measure_auto.py ─────────────────
    runner = ChaosRunner(
        Scenario("ssh-storage-measure", faults=[
            StorageCorrupt(
                directory=SRC_DIR,
                pattern="*.pdb",
                interval="5s",
                recursive=False,
            )
        ]),
        target=target,
        auto_install=True,
        conflict="force",
    )

    print(f"\n  source : {SRC_DIR}  ({len(os.listdir(SRC_DIR))} files)")
    print("  Running measure() — 3 baseline, 3 fault trials ...\n")

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
    print(f"  target             : {target.__class__.__name__}")
    print(f"  session id         : {result.session_id}")

    shutil.rmtree(SRC_DIR, ignore_errors=True)
    shutil.rmtree(DEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
