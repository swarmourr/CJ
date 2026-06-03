#!/usr/bin/env python3
"""
Scenario 4 — Silent Network Corruption Detection
=================================================
Proves that BPF/XDP silent corruption is INVISIBLE at the TCP transport
layer but DETECTABLE at the application layer via checksums.

TCP reports no error (checksum was recalculated by the BPF hook).
SHA-256 of the received file does NOT match the sender.

This simulates real hardware bit-flip faults on scientific data transfers.

Workload : send a file over a real TCP socket (localhost), verify SHA-256
Fault    : SilentNetworkCorrupt(rate=1) — corrupt every packet
Measures :
  tcp_errors       — should be 0 (TCP layer sees nothing)
  checksum_match   — should be False (application catches it)
  detection_layer  — "application" (proves TCP was fooled)

Requirements:
  - Linux with BCC installed
  - sudo

Usage:
    sudo python3 examples/silent_corrupt_detect.py
"""

import hashlib
import os
import socket
import tempfile
import threading
import time

from chaos_jungle import Scenario, ChaosRunner
from chaos_jungle.faults import SilentNetworkCorrupt
from chaos_jungle.targets import LocalTarget

PORT      = 19876
CHUNK     = 4096
FILE_SIZE = 64 * 1024   # 64 KB


# ── TCP server / client ────────────────────────────────────────────────────

def _server(path: str, ready: threading.Event):
    """Receive data over TCP and write to path."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", PORT))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        with conn, open(path, "wb") as fh:
            while True:
                data = conn.recv(CHUNK)
                if not data:
                    break
                fh.write(data)


def _client(src_path: str) -> int:
    """Send file over TCP. Returns 0 on success, 1 on TCP-level error."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect(("127.0.0.1", PORT))
            with open(src_path, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    s.sendall(chunk)
        return 0
    except Exception:
        return 1


def transfer_once(src_path: str) -> tuple[str, int]:
    """Transfer src_path over TCP, return (received_sha256, tcp_errors)."""
    dst_path = tempfile.mktemp(suffix=".recv")
    ready    = threading.Event()
    t = threading.Thread(target=_server, args=(dst_path, ready), daemon=True)
    t.start()
    ready.wait(timeout=2)

    tcp_errors = _client(src_path)
    t.join(timeout=5)

    h = hashlib.sha256()
    if os.path.exists(dst_path):
        with open(dst_path, "rb") as fh:
            h.update(fh.read())
        os.remove(dst_path)

    return h.hexdigest(), tcp_errors


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    # Create a source file with known content
    src = tempfile.mktemp(suffix=".bin")
    with open(src, "wb") as fh:
        fh.write(os.urandom(FILE_SIZE))

    src_sha256 = hashlib.sha256(open(src, "rb").read()).hexdigest()
    print(f"\n  source sha256 : {src_sha256[:16]}...")
    print(f"  file size     : {FILE_SIZE // 1024} KB")

    # ── Baseline transfer (no fault) ─────────────────────────────────────
    print("\n  [baseline] Transferring without fault ...")
    recv_sha256, tcp_err = transfer_once(src)
    baseline_match = recv_sha256 == src_sha256
    print(f"  tcp_errors     : {tcp_err}")
    print(f"  checksum_match : {baseline_match}  (expected True)")

    # ── Fault transfer ────────────────────────────────────────────────────
    runner = ChaosRunner(
        Scenario("silent-corrupt", faults=[
            SilentNetworkCorrupt(rate=1, hook="tc"),
        ]),
        target=LocalTarget(),
        auto_install=True,
        conflict="force",
    )

    print("\n  [fault] Injecting SilentNetworkCorrupt(rate=1) ...")
    runner.start()
    time.sleep(1)   # give BPF hook time to attach

    tcp_errors_total = 0
    checksum_failures = 0
    trials = 5

    for i in range(trials):
        recv_sha256, tcp_err = transfer_once(src)
        tcp_errors_total += tcp_err
        match = recv_sha256 == src_sha256
        if not match:
            checksum_failures += 1
        print(f"  trial {i+1}: tcp_errors={tcp_err}  checksum_match={match}")

    runner.stop()

    # ── Report ────────────────────────────────────────────────────────────
    print("\n" + "=" * 58)
    print("  SILENT CORRUPTION RESULT")
    print("=" * 58)
    print(f"  trials             : {trials}")
    print(f"  tcp_errors total   : {tcp_errors_total}  (should be 0)")
    print(f"  checksum failures  : {checksum_failures}/{trials}")
    print(f"  detection_layer    : {'application' if checksum_failures > 0 else 'none'}")
    print("=" * 58)
    print(f"\n  TCP was fooled          : {tcp_errors_total == 0}")
    print(f"  Application detected it : {checksum_failures > 0}")
    print(f"\n  This is what real hardware bit-flips look like.")

    runner.record_result({
        "trials":             trials,
        "tcp_errors":         tcp_errors_total,
        "checksum_failures":  checksum_failures,
        "tcp_fooled":         int(tcp_errors_total == 0),
        "app_detected":       int(checksum_failures > 0),
    })

    os.remove(src)


if __name__ == "__main__":
    main()
