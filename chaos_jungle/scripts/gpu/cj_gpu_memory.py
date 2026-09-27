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


class GPUMemoryAllocator:
    """Allocate and hold a fixed percentage of GPU VRAM using the CUDA driver API.

    Parameters
    ----------
    memory_pct : float
        Percentage of total VRAM to allocate (0–100).
    gpu_id : int
        CUDA device index.

    Examples
    --------
    ::

        alloc = GPUMemoryAllocator(memory_pct=80.0, gpu_id=0)
        alloc.run()   # blocks until SIGTERM/SIGINT
    """

    def __init__(self, memory_pct: float = 80.0, gpu_id: int = 0) -> None:
        self.memory_pct = memory_pct
        self.gpu_id = gpu_id
        self._cuda: ctypes.CDLL | None = None
        self._ctx: ctypes.c_void_p | None = None
        self._ptr: ctypes.c_void_p | None = None
        self._alloc_mb: int = 0

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _load_cuda() -> ctypes.CDLL:
        for name in ("libcuda.so.1", "libcuda.so"):
            try:
                return ctypes.CDLL(name)
            except OSError:
                continue
        print("[GPUMemoryAllocator] ERROR: libcuda.so not found — is CUDA installed?",
              file=sys.stderr)
        sys.exit(1)

    @staticmethod
    def _total_vram_mb(gpu_id: int) -> int:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
                f"--id={gpu_id}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"[GPUMemoryAllocator] ERROR: nvidia-smi failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            sys.exit(1)
        return int(result.stdout.strip())

    # ── Lifecycle ─────────────────────────────────────────────────

    def allocate(self) -> None:
        """Initialise CUDA and allocate VRAM. Call before :meth:`run`."""
        self._cuda = self._load_cuda()
        total_mb = self._total_vram_mb(self.gpu_id)
        self._alloc_mb = int(total_mb * self.memory_pct / 100)
        alloc_bytes = self._alloc_mb * 1024 * 1024

        ret = self._cuda.cuInit(0)
        if ret != 0:
            print(f"[GPUMemoryAllocator] cuInit failed: {ret}", file=sys.stderr)
            sys.exit(1)

        self._ctx = ctypes.c_void_p()
        ret = self._cuda.cuCtxCreate_v2(ctypes.byref(self._ctx), 0, self.gpu_id)
        if ret != 0:
            print(f"[GPUMemoryAllocator] cuCtxCreate failed: {ret}", file=sys.stderr)
            sys.exit(1)

        self._ptr = ctypes.c_void_p()
        ret = self._cuda.cuMemAlloc_v2(ctypes.byref(self._ptr), alloc_bytes)
        if ret != 0:
            print(
                f"[GPUMemoryAllocator] cuMemAlloc failed (code {ret}) — "
                f"requested {self._alloc_mb}MB on GPU {self.gpu_id}",
                file=sys.stderr,
            )
            self._cuda.cuCtxDestroy_v2(self._ctx)
            sys.exit(1)

        print(
            f"[GPUMemoryAllocator] Holding {self._alloc_mb}MB "
            f"({self.memory_pct:.0f}%) on GPU {self.gpu_id}",
            flush=True,
        )

    def release(self) -> None:
        """Free the VRAM allocation and destroy the CUDA context."""
        if self._cuda and self._ptr:
            self._cuda.cuMemFree_v2(self._ptr)
        if self._cuda and self._ctx:
            self._cuda.cuCtxDestroy_v2(self._ctx)
        print(
            f"[GPUMemoryAllocator] Released {self._alloc_mb}MB on GPU {self.gpu_id}",
            flush=True,
        )

    def run(self) -> None:
        """Allocate VRAM and block until SIGTERM or SIGINT."""
        self.allocate()

        def _cleanup(sig, frame):
            self.release()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _cleanup)
        signal.signal(signal.SIGINT, _cleanup)

        while True:
            time.sleep(1)


# ── CLI entry-point ───────────────────────────────────────────────

def main() -> None:
    pct    = float(sys.argv[1]) if len(sys.argv) > 1 else 80.0
    gpu_id = int(sys.argv[2])   if len(sys.argv) > 2 else 0
    GPUMemoryAllocator(memory_pct=pct, gpu_id=gpu_id).run()


if __name__ == "__main__":
    main()
