"""Resource exhaustion fault implementations.

These faults consume system resources (CPU, memory, disk, I/O) to
simulate the degraded-node scenarios that tools like Chaos Monkey
address by instance termination — but without destroying the machine.

All faults require an SSHTarget (or a LocalTarget with appropriate
permissions) and ``stress-ng`` or standard POSIX utilities.

Classes
-------
DiskFull      — fill a filesystem to near-capacity using ``dd``
CPUStress     — saturate CPU cores using ``stress-ng``
MemoryStress  — allocate memory to simulate memory pressure
IOStress      — generate disk I/O load using ``stress-ng``
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# Sentinel file name written by DiskFull so revert() can clean up
_FILL_FILENAME = ".cj_diskfill"


class DiskFull(Fault):
    """Fill a filesystem to simulate disk-full conditions.

    Creates a file of ``size_mb`` MiB using ``dd if=/dev/zero`` at
    ``path``.  ``stop()`` / ``revert()`` delete the fill file,
    restoring free space.

    Parameters
    ----------
    path : str
        Absolute path to a directory on the filesystem to fill.
        Default ``"/tmp"``.
    size_mb : int
        Size in MiB to write. Default ``2048`` (2 GiB).

    Examples
    --------
    >>> fault = DiskFull("/var/lib/data", size_mb=10000)
    """

    dependencies: list[str] = ["coreutils"]
    danger_level: int = 2  # destructive — fills disk, requires cleanup

    def __init__(self, path: str = "/tmp", size_mb: int = 2048) -> None:
        if not path.startswith("/"):
            raise ValueError(
                f"DiskFull 'path' must be an absolute path, got {path!r}."
            )
        if size_mb < 1:
            raise ValueError(
                f"DiskFull 'size_mb' must be ≥ 1, got {size_mb}."
            )
        self.path = path.rstrip("/")
        self.size_mb = size_mb
        self._fill_file = f"{self.path}/{_FILL_FILENAME}"

    def start(self, target: "Target") -> None:
        target.run(
            f"dd if=/dev/zero of={self._fill_file} bs=1M count={self.size_mb} "
            f"2>/dev/null || true"
        )

    def stop(self, target: "Target") -> None:
        target.run(f"rm -f {self._fill_file}")

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"path": self.path, "size_mb": self.size_mb}


class CPUStress(Fault):
    """Saturate CPU cores to simulate high-CPU conditions.

    Runs ``stress-ng --cpu`` in the background.  ``stop()`` kills the
    stress-ng process.

    Parameters
    ----------
    cores : int
        Number of CPU stressor workers. Default ``1``.
    duration_s : int
        Maximum duration in seconds before stress-ng exits on its own.
        Set to a large number (e.g. ``3600``) to keep it running until
        ``stop()`` is called.  Default ``120``.

    Examples
    --------
    >>> fault = CPUStress(cores=4)
    >>> fault = CPUStress(cores=2, duration_s=300)
    """

    dependencies: list[str] = ["stress-ng"]
    danger_level: int = 1  # moderate — saturates CPU, affects co-located workloads

    def __init__(self, cores: int = 1, duration_s: int = 120) -> None:
        if cores < 1:
            raise ValueError(f"CPUStress 'cores' must be ≥ 1, got {cores}.")
        if duration_s < 1:
            raise ValueError(f"CPUStress 'duration_s' must be ≥ 1, got {duration_s}.")
        self.cores = cores
        self.duration_s = duration_s

    def start(self, target: "Target") -> None:
        target.run(
            f"nohup stress-ng --cpu {self.cores} --timeout {self.duration_s}s "
            f"--metrics-brief > /tmp/cj_cpu_stress.log 2>&1 &"
        )

    def stop(self, target: "Target") -> None:
        target.run("pkill -f 'stress-ng --cpu' 2>/dev/null || true")
        target.run("rm -f /tmp/cj_cpu_stress.log")

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"cores": self.cores, "duration_s": self.duration_s}


class MemoryStress(Fault):
    """Allocate memory to simulate memory pressure.

    Runs ``stress-ng --vm`` in the background, allocating ``mb`` MiB.
    ``stop()`` kills the stress-ng process, releasing memory.

    Parameters
    ----------
    mb : int
        Amount of memory (in MiB) to allocate. Default ``512``.
    duration_s : int
        Maximum auto-exit duration in seconds. Default ``120``.

    Examples
    --------
    >>> fault = MemoryStress(mb=4096)
    """

    dependencies: list[str] = ["stress-ng"]
    danger_level: int = 1  # moderate — allocates system RAM, may OOM co-located processes

    def __init__(self, mb: int = 512, duration_s: int = 120) -> None:
        if mb < 1:
            raise ValueError(f"MemoryStress 'mb' must be ≥ 1, got {mb}.")
        if duration_s < 1:
            raise ValueError(f"MemoryStress 'duration_s' must be ≥ 1, got {duration_s}.")
        self.mb = mb
        self.duration_s = duration_s

    def start(self, target: "Target") -> None:
        target.run(
            f"nohup stress-ng --vm 1 --vm-bytes {self.mb}M "
            f"--timeout {self.duration_s}s "
            f"> /tmp/cj_mem_stress.log 2>&1 &"
        )

    def stop(self, target: "Target") -> None:
        target.run("pkill -f 'stress-ng --vm' 2>/dev/null || true")
        target.run("rm -f /tmp/cj_mem_stress.log")

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"mb": self.mb, "duration_s": self.duration_s}


class IOStress(Fault):
    """Generate disk I/O load to simulate a busy storage subsystem.

    Runs ``stress-ng --hdd`` workers in the background, continuously
    writing and reading temporary files under ``path``.

    Parameters
    ----------
    workers : int
        Number of I/O stressor workers. Default ``1``.
    duration_s : int
        Maximum auto-exit duration in seconds. Default ``120``.
    path : str
        Directory to write stress files into. Default ``"/tmp"``.

    Examples
    --------
    >>> fault = IOStress(workers=2, path="/var/lib/data")
    """

    dependencies: list[str] = ["stress-ng"]

    def __init__(
        self,
        workers: int = 1,
        duration_s: int = 120,
        path: str = "/tmp",
    ) -> None:
        if workers < 1:
            raise ValueError(f"IOStress 'workers' must be ≥ 1, got {workers}.")
        if duration_s < 1:
            raise ValueError(f"IOStress 'duration_s' must be ≥ 1, got {duration_s}.")
        if not path.startswith("/"):
            raise ValueError(f"IOStress 'path' must be absolute, got {path!r}.")
        self.workers = workers
        self.duration_s = duration_s
        self.path = path.rstrip("/")

    def start(self, target: "Target") -> None:
        target.run(
            f"nohup stress-ng --hdd {self.workers} --hdd-dir {self.path} "
            f"--timeout {self.duration_s}s "
            f"> /tmp/cj_io_stress.log 2>&1 &"
        )

    def stop(self, target: "Target") -> None:
        target.run("pkill -f 'stress-ng --hdd' 2>/dev/null || true")
        target.run("rm -f /tmp/cj_io_stress.log")

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"workers": self.workers, "duration_s": self.duration_s, "path": self.path}
