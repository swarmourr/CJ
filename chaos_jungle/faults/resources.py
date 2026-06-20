"""Resource exhaustion fault implementations."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

_FILL_FILENAME = ".cj_diskfill"


class DiskFull(Fault):
    """Fill a filesystem to simulate disk-full conditions.

    Parameters
    ----------
    path : str
        Absolute path to a directory on the filesystem to fill. Default ``"/tmp"``.
    size_mb : int
        Size in MiB to write. Default ``2048`` (2 GiB).

    Examples
    --------
    >>> fault = DiskFull("/var/lib/data", size_mb=10000)
    """

    dependencies: list[str]    = ["coreutils"]
    danger_level: int          = 2
    default_metrics: list[str] = ["disk_used_bytes", "write_errors", "read_errors", "inode_used", "duration_s"]

    def __init__(self, path: str = "/tmp", size_mb: int = 2048) -> None:
        if not path.startswith("/"):
            raise ValueError(f"DiskFull 'path' must be an absolute path, got {path!r}.")
        if size_mb < 1:
            raise ValueError(f"DiskFull 'size_mb' must be ≥ 1, got {size_mb}.")
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

    Parameters
    ----------
    cores : int
        Number of CPU stressor workers. Default ``1``.
    duration_s : int
        Maximum auto-exit duration in seconds. Default ``120``.

    Examples
    --------
    >>> fault = CPUStress(cores=4)
    """

    dependencies: list[str]    = ["stress-ng"]
    danger_level: int          = 1
    default_metrics: list[str] = ["cpu_percent", "context_switches", "duration_s", "process_wait_ms"]

    def __init__(self, cores: int = 1, duration_s: int = 120) -> None:
        if cores < 1:
            raise ValueError(f"CPUStress 'cores' must be ≥ 1, got {cores}.")
        if duration_s < 1:
            raise ValueError(f"CPUStress 'duration_s' must be ≥ 1, got {duration_s}.")
        self.cores = cores
        self.duration_s = duration_s
        # Unique PID file so concurrent instances don't kill each other
        self._pid_file = f"/tmp/cj_cpu_stress_{uuid.uuid4().hex[:8]}.pid"

    def start(self, target: "Target") -> None:
        target.run(
            f"nohup stress-ng --cpu {self.cores} --timeout {self.duration_s}s "
            f"--metrics-brief > /tmp/cj_cpu_stress.log 2>&1 & echo $! > {self._pid_file}"
        )

    def stop(self, target: "Target") -> None:
        target.run(
            f"kill $(cat {self._pid_file} 2>/dev/null) 2>/dev/null || true && "
            f"rm -f {self._pid_file} /tmp/cj_cpu_stress.log"
        )

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"cores": self.cores, "duration_s": self.duration_s}


class MemoryStress(Fault):
    """Allocate memory to simulate memory pressure.

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

    dependencies: list[str]    = ["stress-ng"]
    danger_level: int          = 1
    default_metrics: list[str] = ["memory_mb", "swap_used_mb", "cpu_percent", "duration_s", "oom_events"]

    def __init__(self, mb: int = 512, duration_s: int = 120) -> None:
        if mb < 1:
            raise ValueError(f"MemoryStress 'mb' must be ≥ 1, got {mb}.")
        if duration_s < 1:
            raise ValueError(f"MemoryStress 'duration_s' must be ≥ 1, got {duration_s}.")
        self.mb = mb
        self.duration_s = duration_s
        self._pid_file = f"/tmp/cj_mem_stress_{uuid.uuid4().hex[:8]}.pid"

    def start(self, target: "Target") -> None:
        target.run(
            f"nohup stress-ng --vm 1 --vm-bytes {self.mb}M "
            f"--timeout {self.duration_s}s "
            f"> /tmp/cj_mem_stress.log 2>&1 & echo $! > {self._pid_file}"
        )

    def stop(self, target: "Target") -> None:
        target.run(
            f"kill $(cat {self._pid_file} 2>/dev/null) 2>/dev/null || true && "
            f"rm -f {self._pid_file} /tmp/cj_mem_stress.log"
        )

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"mb": self.mb, "duration_s": self.duration_s}


class IOStress(Fault):
    """Generate disk I/O load to simulate a busy storage subsystem.

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

    dependencies: list[str]    = ["stress-ng"]
    default_metrics: list[str] = ["iops", "io_wait_ms", "read_latency_ms", "write_latency_ms", "duration_s"]

    def __init__(self, workers: int = 1, duration_s: int = 120, path: str = "/tmp") -> None:
        if workers < 1:
            raise ValueError(f"IOStress 'workers' must be ≥ 1, got {workers}.")
        if duration_s < 1:
            raise ValueError(f"IOStress 'duration_s' must be ≥ 1, got {duration_s}.")
        if not path.startswith("/"):
            raise ValueError(f"IOStress 'path' must be absolute, got {path!r}.")
        self.workers = workers
        self.duration_s = duration_s
        self.path = path.rstrip("/")
        self._pid_file = f"/tmp/cj_io_stress_{uuid.uuid4().hex[:8]}.pid"

    def start(self, target: "Target") -> None:
        target.run(
            f"nohup stress-ng --hdd {self.workers} --hdd-dir {self.path} "
            f"--timeout {self.duration_s}s "
            f"> /tmp/cj_io_stress.log 2>&1 & echo $! > {self._pid_file}"
        )

    def stop(self, target: "Target") -> None:
        target.run(
            f"kill $(cat {self._pid_file} 2>/dev/null) 2>/dev/null || true && "
            f"rm -f {self._pid_file} /tmp/cj_io_stress.log"
        )

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"workers": self.workers, "duration_s": self.duration_s, "path": self.path}


class InodeFull(Fault):
    """Exhaust filesystem inodes by creating many tiny files.

    A filesystem can have free disk space but zero inodes — processes
    then cannot create new files even though ``df`` shows space available.
    This is a common silent failure mode for log-heavy or temp-file-heavy
    workloads.

    ``stop()`` / ``revert()`` remove the fill directory and all created files.

    Parameters
    ----------
    path : str
        Absolute path to a directory on the target filesystem to fill.
        Default ``"/tmp"``.
    count : int
        Number of zero-byte files to create. Default ``500_000``.

    Examples
    --------
    >>> fault = InodeFull("/var/log/app", count=200_000)
    """

    dependencies: list[str]    = ["coreutils"]
    danger_level: int          = 2
    default_metrics: list[str] = ["inode_used", "write_errors", "duration_s"]

    def __init__(self, path: str = "/tmp", count: int = 500_000) -> None:
        if not path.startswith("/"):
            raise ValueError(f"InodeFull 'path' must be an absolute path, got {path!r}.")
        if count < 1:
            raise ValueError(f"InodeFull 'count' must be ≥ 1, got {count}.")
        self.path = path.rstrip("/")
        self.count = count
        self._fill_dir = f"{self.path}/.cj_inodefill_{uuid.uuid4().hex[:8]}"

    def start(self, target: "Target") -> None:
        target.run(
            f"mkdir -p {self._fill_dir} && "
            f"seq 1 {self.count} | xargs -P4 -I{{}} touch {self._fill_dir}/f_{{}} 2>/dev/null || true"
        )

    def stop(self, target: "Target") -> None:
        target.run(f"rm -rf {self._fill_dir} 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"path": self.path, "count": self.count}


class FDExhaust(Fault):
    """Exhaust file descriptor limits by holding many open FDs.

    Starts a background Python process that opens ``count`` file
    descriptors (``/dev/null``) and holds them open until ``stop()``
    is called.  When the system or per-process ``ulimit -n`` is hit,
    subsequent ``open()`` / ``socket()`` calls fail with
    ``EMFILE`` / ``Too many open files``.

    Parameters
    ----------
    count : int
        Number of file descriptors to open and hold. Default ``60_000``.

    Examples
    --------
    >>> fault = FDExhaust(count=60_000)
    """

    danger_level: int          = 2
    default_metrics: list[str] = ["open_fds", "write_errors", "error_rate", "duration_s"]

    def __init__(self, count: int = 60_000) -> None:
        if count < 1:
            raise ValueError(f"FDExhaust 'count' must be ≥ 1, got {count}.")
        self.count = count
        self._pid_file = f"/tmp/cj_fd_exhaust_{uuid.uuid4().hex[:8]}.pid"

    def start(self, target: "Target") -> None:
        script = (
            f"import time; "
            f"fds = [open('/dev/null') for _ in range({self.count})]; "
            f"time.sleep(86400)"
        )
        target.run(
            f"nohup python3 -c {script!r} > /dev/null 2>&1 & echo $! > {self._pid_file}"
        )

    def stop(self, target: "Target") -> None:
        target.run(
            f"kill $(cat {self._pid_file} 2>/dev/null) 2>/dev/null || true && "
            f"rm -f {self._pid_file}"
        )

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"count": self.count}


class ProcessExhaust(Fault):
    """Exhaust the kernel PID limit by forking many short-lived processes.

    Spawns ``count`` background ``sleep`` subprocesses, each waiting 24 h.
    When the system PID namespace limit (``/proc/sys/kernel/pid_max``) is
    approached, ``fork()`` calls fail with ``EAGAIN``.  Services that need
    to spawn subprocesses (agents with tool executors, gunicorn workers,
    etc.) will fail to start new processes.

    ``stop()`` sends SIGKILL to the parent bash process which takes down
    all children via their process group.

    Parameters
    ----------
    count : int
        Number of background processes to spawn. Default ``5_000``.

    Examples
    --------
    >>> fault = ProcessExhaust(count=5_000)
    """

    danger_level: int          = 3
    default_metrics: list[str] = ["process_count", "error_rate", "duration_s"]

    def __init__(self, count: int = 5_000) -> None:
        if count < 1:
            raise ValueError(f"ProcessExhaust 'count' must be ≥ 1, got {count}.")
        self.count = count
        self._pid_file = f"/tmp/cj_proc_exhaust_{uuid.uuid4().hex[:8]}.pid"

    def start(self, target: "Target") -> None:
        # Spawn a parent bash that forks count sleep processes; save parent PID
        target.run(
            f"nohup bash -c '"
            f"for i in $(seq 1 {self.count}); do sleep 86400 & done; wait"
            f"' > /dev/null 2>&1 & echo $! > {self._pid_file}"
        )

    def stop(self, target: "Target") -> None:
        # Kill the process group so all children die with the parent
        target.run(
            f"pid=$(cat {self._pid_file} 2>/dev/null); "
            f"[ -n \"$pid\" ] && kill -KILL -$pid 2>/dev/null || kill $pid 2>/dev/null; "
            f"rm -f {self._pid_file}"
        )

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {"count": self.count}
