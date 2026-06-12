"""GPU fault implementations — NVIDIA, AMD, and Intel.

All faults auto-detect the installed GPU vendor at ``start()`` time
and dispatch to the appropriate backend.  No manual configuration is
needed.

Vendor detection order
----------------------
1. **NVIDIA** — ``nvidia-smi`` present and reports a GPU
2. **AMD**    — ``rocm-smi`` present, or AMD sysfs power-cap node found
3. **Intel**  — Intel GT sysfs frequency nodes found

Classes
-------
GPUThrottle       — reduce power/frequency to simulate thermal throttling
GPUMemoryPressure — allocate VRAM to force workloads into OOM / slow paths
GPUClockLock      — lock clocks to minimum to simulate sustained degradation
"""
from __future__ import annotations

import re
from importlib.resources import files, as_file
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# ── Vendor detection ──────────────────────────────────────────────────────────

def _detect_vendor(target: "Target") -> str:
    """Return ``'nvidia'``, ``'amd'``, ``'intel'``, or ``'unknown'``."""
    _, out, _ = target.run(
        "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1"
    )
    if out.strip():
        return "nvidia"

    _, out, _ = target.run(
        "rocm-smi --showproductname 2>/dev/null | grep -i 'gpu\\|amd\\|radeon' | head -1"
    )
    if out.strip():
        return "amd"

    # AMD sysfs fallback (no rocm-smi but kernel driver present)
    _, out, _ = target.run(
        "ls /sys/class/drm/card*/device/hwmon/hwmon*/power1_cap 2>/dev/null | head -1"
    )
    if out.strip():
        return "amd"

    # Intel GT sysfs
    _, out, _ = target.run(
        "ls /sys/class/drm/card*/gt/gt0/rps_max_freq_mhz 2>/dev/null | head -1"
    )
    if out.strip():
        return "intel"

    return "unknown"


# ── AMD sysfs helpers ─────────────────────────────────────────────────────────

def _amd_power_cap_path(target: "Target") -> str:
    """Return the sysfs path to the first AMD GPU power1_cap node."""
    _, out, _ = target.run(
        "ls /sys/class/drm/card*/device/hwmon/hwmon*/power1_cap 2>/dev/null | head -1"
    )
    path = out.strip()
    if not path:
        raise RuntimeError(
            "[GPU] AMD power cap sysfs node not found. "
            "Make sure the amdgpu kernel module is loaded."
        )
    return path


def _amd_card_path(target: "Target") -> str:
    """Return the sysfs base path for the first AMD drm card."""
    _, out, _ = target.run(
        "ls -d /sys/class/drm/card*/device 2>/dev/null | head -1"
    )
    path = out.strip()
    if not path:
        raise RuntimeError("[GPU] AMD drm card sysfs path not found.")
    return path


# ── Intel sysfs helpers ───────────────────────────────────────────────────────

def _intel_gt_path(target: "Target") -> str:
    """Return the sysfs path to the first Intel GT frequency directory."""
    _, out, _ = target.run(
        "ls /sys/class/drm/card*/gt/gt0/rps_max_freq_mhz 2>/dev/null | head -1"
    )
    path = out.strip()
    if not path:
        raise RuntimeError(
            "[GPU] Intel GT sysfs frequency node not found. "
            "Make sure the i915/xe kernel module is loaded."
        )
    return path.replace("/rps_max_freq_mhz", "")


# ─────────────────────────────────────────────────────────────────────────────
# GPUThrottle
# ─────────────────────────────────────────────────────────────────────────────

class GPUThrottle(Fault):
    """Reduce GPU power or frequency cap to simulate thermal throttling.

    Auto-detects the GPU vendor and uses the appropriate backend:

    * **NVIDIA** — ``nvidia-smi -pl <watts>``
    * **AMD**    — writes to ``/sys/…/hwmon*/power1_cap`` (microwatts)
    * **Intel**  — writes to ``/sys/…/gt/gt0/rps_max_freq_mhz``

    The original limit is queried at ``start()`` and fully restored by
    ``revert()``.

    Parameters
    ----------
    power_pct : float
        Target limit as a percentage of the GPU's maximum.
        E.g. ``50`` caps at half TDP / half max frequency. Default ``50``.
    gpu_id : int
        GPU index (as shown by ``nvidia-smi -L`` / ``rocm-smi -i``).
        Default ``0``.

    Notes
    -----
    Requires ``sudo`` on the target for all vendors.

    Examples
    --------
    >>> fault = GPUThrottle(power_pct=40)
    >>> fault = GPUThrottle(power_pct=60, gpu_id=1)
    """

    dependencies: list[str] = []   # checked dynamically per vendor

    def __init__(self, power_pct: float = 50.0, gpu_id: int = 0) -> None:
        if not (1.0 <= power_pct <= 100.0):
            raise ValueError(
                f"GPUThrottle 'power_pct' must be between 1 and 100, got {power_pct}."
            )
        if gpu_id < 0:
            raise ValueError(f"GPUThrottle 'gpu_id' must be >= 0, got {gpu_id}.")
        self.power_pct = power_pct
        self.gpu_id = gpu_id
        self._vendor: str | None = None
        self._original: float | None = None   # watts / microwatts / MHz depending on vendor

    # ── NVIDIA ────────────────────────────────────────────────────

    def _start_nvidia(self, target: "Target") -> None:
        _, out, _ = target.run(
            f"nvidia-smi -q -d POWER --id={self.gpu_id} "
            f"| grep -E 'Current Power Limit|Max Power Limit'"
        )
        current = max_power = None
        for line in out.splitlines():
            m = re.search(r"([\d.]+)\s*W", line)
            if not m:
                continue
            val = float(m.group(1))
            if "Current" in line:
                current = val
            elif "Max" in line:
                max_power = val
        if current is None or max_power is None:
            raise RuntimeError(f"[GPUThrottle] Cannot parse NVIDIA power limits:\n{out}")
        self._original = current
        target_w = round(max_power * self.power_pct / 100.0, 1)
        target.sudo(f"nvidia-smi -pl {target_w} --id={self.gpu_id}")

    def _revert_nvidia(self, target: "Target") -> None:
        if self._original is not None:
            target.sudo(f"nvidia-smi -pl {self._original} --id={self.gpu_id}")

    # ── AMD ───────────────────────────────────────────────────────

    def _start_amd(self, target: "Target") -> None:
        cap_path = _amd_power_cap_path(target)
        _, cap_str, _ = target.run(f"cat {cap_path}_max")
        max_uw = int(cap_str.strip())
        _, cur_str, _ = target.run(f"cat {cap_path}")
        self._original = int(cur_str.strip())
        target_uw = int(max_uw * self.power_pct / 100.0)
        target.sudo(f"echo {target_uw} > {cap_path}")

    def _revert_amd(self, target: "Target") -> None:
        if self._original is not None:
            cap_path = _amd_power_cap_path(target)
            target.sudo(f"echo {int(self._original)} > {cap_path}")

    # ── Intel ─────────────────────────────────────────────────────

    def _start_intel(self, target: "Target") -> None:
        gt = _intel_gt_path(target)
        _, cur_str, _ = target.run(f"cat {gt}/rps_max_freq_mhz")
        _, max_str, _ = target.run(f"cat {gt}/rps_rp0_freq_mhz 2>/dev/null || cat {gt}/rps_max_freq_mhz")
        max_mhz = int(max_str.strip())
        self._original = int(cur_str.strip())
        target_mhz = int(max_mhz * self.power_pct / 100.0)
        target.sudo(f"echo {target_mhz} > {gt}/rps_max_freq_mhz")

    def _revert_intel(self, target: "Target") -> None:
        if self._original is not None:
            gt = _intel_gt_path(target)
            target.sudo(f"echo {int(self._original)} > {gt}/rps_max_freq_mhz")

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, target: "Target") -> None:
        self._vendor = _detect_vendor(target)
        if self._vendor == "nvidia":
            self._start_nvidia(target)
        elif self._vendor == "amd":
            self._start_amd(target)
        elif self._vendor == "intel":
            self._start_intel(target)
        else:
            raise RuntimeError(
                "[GPUThrottle] No supported GPU found. "
                "Expected NVIDIA (nvidia-smi), AMD (rocm-smi / amdgpu), "
                "or Intel (i915/xe with GT sysfs)."
            )

    def stop(self, target: "Target") -> None:
        pass

    def revert(self, target: "Target") -> None:
        if self._vendor == "nvidia":
            self._revert_nvidia(target)
        elif self._vendor == "amd":
            self._revert_amd(target)
        elif self._vendor == "intel":
            self._revert_intel(target)

    def _parameters(self) -> dict:
        return {"power_pct": self.power_pct, "gpu_id": self.gpu_id}


# ─────────────────────────────────────────────────────────────────────────────
# GPUMemoryPressure
# ─────────────────────────────────────────────────────────────────────────────

class GPUMemoryPressure(Fault):
    """Allocate GPU VRAM to force workloads into OOM or slow memory paths.

    Auto-detects vendor and uses the appropriate bundled background script:

    * **NVIDIA** — ``cj_gpu_memory.py``     (CUDA ctypes / ``libcuda.so``)
    * **AMD**    — ``cj_gpu_memory_amd.py`` (HIP ctypes  / ``libamdhip64.so``)
    * **Intel**  — not supported (iGPU shares system RAM; use
      :class:`~chaos_jungle.faults.resources.MemoryStress` instead)

    Both scripts are **bundled inside the package** and auto-deployed to
    ``~/.chaos-jungle/gpu/`` on the target.

    Parameters
    ----------
    memory_pct : float
        Percentage of total VRAM to hold. Default ``80``.
    gpu_id : int
        GPU index. Default ``0``.

    Examples
    --------
    >>> fault = GPUMemoryPressure(memory_pct=90)
    >>> fault = GPUMemoryPressure(memory_pct=50, gpu_id=1)
    """

    dependencies: list[str] = []

    def __init__(self, memory_pct: float = 80.0, gpu_id: int = 0) -> None:
        if not (1.0 <= memory_pct <= 99.0):
            raise ValueError(
                f"GPUMemoryPressure 'memory_pct' must be between 1 and 99, got {memory_pct}."
            )
        if gpu_id < 0:
            raise ValueError(f"GPUMemoryPressure 'gpu_id' must be >= 0, got {gpu_id}.")
        self.memory_pct = memory_pct
        self.gpu_id = gpu_id
        self._pid_file = "/tmp/cj_gpu_mem.pid"
        self._vendor: str | None = None
        self._deployed_dir: str | None = None

    def _deploy_scripts(self, target: "Target") -> str:
        """Upload both GPU memory scripts to target. Return remote dir path."""
        _, home, _ = target.run("echo $HOME")
        home = home.strip() or "/root"
        remote_dir = f"{home}/.chaos-jungle/gpu"
        target.run(f"mkdir -p {remote_dir}")

        gpu_pkg = files("chaos_jungle.scripts.gpu")
        for name in ("cj_gpu_memory.py", "cj_gpu_memory_amd.py"):
            with as_file(gpu_pkg / name) as src:
                target.put(str(src), f"{remote_dir}/{name}")

        self._deployed_dir = remote_dir
        return remote_dir

    def _get_dir(self, target: "Target") -> str:
        if self._deployed_dir is None:
            self._deploy_scripts(target)
        return self._deployed_dir  # type: ignore[return-value]

    def start(self, target: "Target") -> None:
        self._vendor = _detect_vendor(target)
        if self._vendor == "intel":
            raise NotImplementedError(
                "[GPUMemoryPressure] Intel iGPU shares system RAM — "
                "use MemoryStress instead to apply memory pressure."
            )
        if self._vendor == "unknown":
            raise RuntimeError(
                "[GPUMemoryPressure] No supported GPU found (NVIDIA or AMD)."
            )

        script_name = (
            "cj_gpu_memory.py" if self._vendor == "nvidia"
            else "cj_gpu_memory_amd.py"
        )
        remote_dir = self._get_dir(target)
        script = f"{remote_dir}/{script_name}"

        target.run(
            f"nohup python3 {script} {self.memory_pct} {self.gpu_id} "
            f"> /tmp/cj_gpu_mem.log 2>&1 & echo $! > {self._pid_file}"
        )

    def stop(self, target: "Target") -> None:
        target.run(
            f"kill $(cat {self._pid_file} 2>/dev/null) 2>/dev/null || true && "
            f"rm -f {self._pid_file} /tmp/cj_gpu_mem.log"
        )

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"memory_pct": self.memory_pct, "gpu_id": self.gpu_id}


# ─────────────────────────────────────────────────────────────────────────────
# GPUClockLock
# ─────────────────────────────────────────────────────────────────────────────

class GPUClockLock(Fault):
    """Lock GPU clocks to a fixed frequency to simulate sustained degradation.

    Auto-detects vendor and uses the appropriate backend:

    * **NVIDIA** — ``nvidia-smi --lock-gpu-clocks`` / ``--reset-gpu-clocks``
    * **AMD**    — ``power_dpm_force_performance_level=manual``,
      ``pp_dpm_sclk`` level ``0``
    * **Intel**  — writes equal values to ``rps_max_freq_mhz`` and
      ``rps_min_freq_mhz``

    ``revert()`` always restores adaptive / auto clocking.

    Parameters
    ----------
    freq_mhz : int or None
        Clock frequency in MHz to lock to.  ``None`` = auto-detect the
        GPU's minimum supported clock. Default ``None``.
    gpu_id : int
        GPU index. Default ``0``.

    Notes
    -----
    Requires ``sudo`` on the target for all vendors.

    Examples
    --------
    >>> fault = GPUClockLock()                  # lock to minimum
    >>> fault = GPUClockLock(freq_mhz=300)
    >>> fault = GPUClockLock(freq_mhz=500, gpu_id=1)
    """

    dependencies: list[str] = []

    def __init__(self, freq_mhz: int | None = None, gpu_id: int = 0) -> None:
        if freq_mhz is not None and freq_mhz < 1:
            raise ValueError(f"GPUClockLock 'freq_mhz' must be >= 1, got {freq_mhz}.")
        if gpu_id < 0:
            raise ValueError(f"GPUClockLock 'gpu_id' must be >= 0, got {gpu_id}.")
        self.freq_mhz = freq_mhz
        self.gpu_id = gpu_id
        self._vendor: str | None = None
        self._locked_freq: int | None = None
        self._original_min: int | None = None   # Intel only
        self._amd_card_path: str | None = None

    # ── NVIDIA ────────────────────────────────────────────────────

    def _min_clock_nvidia(self, target: "Target") -> int:
        _, out, _ = target.run(
            f"nvidia-smi --query-supported-clocks=graphics "
            f"--format=csv,noheader,nounits --id={self.gpu_id} | tail -1"
        )
        val = out.strip()
        if not val.isdigit():
            raise RuntimeError(
                f"[GPUClockLock] Cannot parse NVIDIA min clock: {val!r}"
            )
        return int(val)

    def _start_nvidia(self, target: "Target") -> None:
        freq = self.freq_mhz if self.freq_mhz is not None else self._min_clock_nvidia(target)
        self._locked_freq = freq
        target.sudo(f"nvidia-smi --lock-gpu-clocks={freq},{freq} --id={self.gpu_id}")

    def _revert_nvidia(self, target: "Target") -> None:
        target.sudo(f"nvidia-smi --reset-gpu-clocks --id={self.gpu_id}")

    # ── AMD ───────────────────────────────────────────────────────

    def _start_amd(self, target: "Target") -> None:
        card = _amd_card_path(target)
        self._amd_card_path = card
        # Force manual performance level so we can set clock level
        target.sudo(f"echo manual > {card}/power_dpm_force_performance_level")
        # Level 0 = minimum clock
        target.sudo(f"echo 0 > {card}/pp_dpm_sclk")

    def _revert_amd(self, target: "Target") -> None:
        card = self._amd_card_path or _amd_card_path(target)
        target.sudo(f"echo auto > {card}/power_dpm_force_performance_level")

    # ── Intel ─────────────────────────────────────────────────────

    def _min_clock_intel(self, target: "Target", gt: str) -> int:
        _, out, _ = target.run(f"cat {gt}/rps_min_freq_mhz")
        return int(out.strip())

    def _start_intel(self, target: "Target") -> None:
        gt = _intel_gt_path(target)
        freq = self.freq_mhz if self.freq_mhz is not None else self._min_clock_intel(target, gt)
        self._locked_freq = freq
        _, cur_min, _ = target.run(f"cat {gt}/rps_min_freq_mhz")
        self._original_min = int(cur_min.strip())
        # Lock both min and max to same value
        target.sudo(f"echo {freq} > {gt}/rps_max_freq_mhz")
        target.sudo(f"echo {freq} > {gt}/rps_min_freq_mhz")

    def _revert_intel(self, target: "Target") -> None:
        gt = _intel_gt_path(target)
        _, rp0, _ = target.run(
            f"cat {gt}/rps_rp0_freq_mhz 2>/dev/null || cat {gt}/rps_max_freq_mhz"
        )
        original_max = int(rp0.strip())
        original_min = self._original_min or 0
        target.sudo(f"echo {original_max} > {gt}/rps_max_freq_mhz")
        target.sudo(f"echo {original_min} > {gt}/rps_min_freq_mhz")

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, target: "Target") -> None:
        self._vendor = _detect_vendor(target)
        if self._vendor == "nvidia":
            self._start_nvidia(target)
        elif self._vendor == "amd":
            self._start_amd(target)
        elif self._vendor == "intel":
            self._start_intel(target)
        else:
            raise RuntimeError(
                "[GPUClockLock] No supported GPU found. "
                "Expected NVIDIA, AMD (amdgpu), or Intel (i915/xe)."
            )

    def stop(self, target: "Target") -> None:
        pass

    def revert(self, target: "Target") -> None:
        if self._vendor == "nvidia":
            self._revert_nvidia(target)
        elif self._vendor == "amd":
            self._revert_amd(target)
        elif self._vendor == "intel":
            self._revert_intel(target)

    def _parameters(self) -> dict:
        return {"freq_mhz": self.freq_mhz, "gpu_id": self.gpu_id}
