#!/usr/bin/env python3
"""Background process that holds a GPU VRAM allocation.

Used by GPUMemoryPressure fault. Allocates a given percentage of GPU
memory via CUDA ctypes (no PyTorch required — only libcuda.so).

Usage:
    python3 cj_gpu_memory.py <memory_pct> <gpu_id>

The process blocks until SIGTERM or SIGINT, then frees the allocation
and exits cleanly.
"""
import ctypes
import signal
import subprocess
import sys
import time


def _load_cuda() -> ctypes.CDLL:
    for name in ("libcuda.so.1", "libcuda.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    print("[cj_gpu_memory] ERROR: libcuda.so not found — is CUDA installed?",
          file=sys.stderr)
    sys.exit(1)


def _total_vram_mb(gpu_id: int) -> int:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.total",
            "--format=csv,noheader,nounits",
            f"--id={gpu_id}",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[cj_gpu_memory] ERROR: nvidia-smi failed: {result.stderr.strip()}",
              file=sys.stderr)
        sys.exit(1)
    return int(result.stdout.strip())


def main() -> None:
    pct    = float(sys.argv[1]) if len(sys.argv) > 1 else 80.0
    gpu_id = int(sys.argv[2])   if len(sys.argv) > 2 else 0

    cuda = _load_cuda()

    total_mb   = _total_vram_mb(gpu_id)
    alloc_mb   = int(total_mb * pct / 100)
    alloc_bytes = alloc_mb * 1024 * 1024

    # Initialise CUDA driver
    ret = cuda.cuInit(0)
    if ret != 0:
        print(f"[cj_gpu_memory] cuInit failed: {ret}", file=sys.stderr)
        sys.exit(1)

    ctx = ctypes.c_void_p()
    ret = cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, gpu_id)
    if ret != 0:
        print(f"[cj_gpu_memory] cuCtxCreate failed: {ret}", file=sys.stderr)
        sys.exit(1)

    # Allocate device memory
    ptr = ctypes.c_void_p()
    ret = cuda.cuMemAlloc_v2(ctypes.byref(ptr), alloc_bytes)
    if ret != 0:
        print(f"[cj_gpu_memory] cuMemAlloc failed (code {ret}) — "
              f"requested {alloc_mb}MB on GPU {gpu_id}", file=sys.stderr)
        cuda.cuCtxDestroy_v2(ctx)
        sys.exit(1)

    print(f"[cj_gpu_memory] Holding {alloc_mb}MB ({pct:.0f}%) on GPU {gpu_id}",
          flush=True)

    def _cleanup(sig, frame):
        cuda.cuMemFree_v2(ptr)
        cuda.cuCtxDestroy_v2(ctx)
        print(f"[cj_gpu_memory] Released {alloc_mb}MB on GPU {gpu_id}", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
