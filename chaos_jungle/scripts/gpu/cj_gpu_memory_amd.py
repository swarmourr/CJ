#!/usr/bin/env python3
"""Background process that holds AMD GPU VRAM allocation.

Used by GPUMemoryPressure fault on AMD GPUs.
Allocates a given percentage of VRAM via HIP ctypes (libamdhip64.so).
No PyTorch or ROCm Python bindings required.

Usage:
    python3 cj_gpu_memory_amd.py <memory_pct> <gpu_id>
"""
import ctypes
import signal
import subprocess
import sys
import time


def _load_hip() -> ctypes.CDLL:
    for name in ("libamdhip64.so.6", "libamdhip64.so.5", "libamdhip64.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    print("[cj_gpu_memory_amd] ERROR: libamdhip64.so not found — is ROCm installed?",
          file=sys.stderr)
    sys.exit(1)


def _total_vram_mb(gpu_id: int) -> int:
    result = subprocess.run(
        ["rocm-smi", "--showmeminfo", "vram", "--id", str(gpu_id)],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if "Total Memory" in line:
            try:
                return int(line.split(":")[-1].strip()) // (1024 * 1024)
            except (ValueError, IndexError):
                pass
    # fallback: 4 GiB
    return 4096


def main() -> None:
    pct    = float(sys.argv[1]) if len(sys.argv) > 1 else 80.0
    gpu_id = int(sys.argv[2])   if len(sys.argv) > 2 else 0

    hip = _load_hip()

    total_mb    = _total_vram_mb(gpu_id)
    alloc_mb    = int(total_mb * pct / 100)
    alloc_bytes = alloc_mb * 1024 * 1024

    hip.hipSetDevice(gpu_id)

    ptr = ctypes.c_void_p()
    ret = hip.hipMalloc(ctypes.byref(ptr), alloc_bytes)
    if ret != 0:
        print(f"[cj_gpu_memory_amd] hipMalloc failed (code {ret}) — "
              f"requested {alloc_mb}MB on AMD GPU {gpu_id}", file=sys.stderr)
        sys.exit(1)

    print(f"[cj_gpu_memory_amd] Holding {alloc_mb}MB ({pct:.0f}%) on AMD GPU {gpu_id}",
          flush=True)

    def _cleanup(sig, frame):
        hip.hipFree(ptr)
        print(f"[cj_gpu_memory_amd] Released {alloc_mb}MB on AMD GPU {gpu_id}", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
