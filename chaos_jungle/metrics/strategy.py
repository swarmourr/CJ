"""CollectStrategy — controls how often metrics are sampled during an experiment."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target
    from chaos_jungle.metrics.schema import MetricSample


# ---------------------------------------------------------------------------
# System metric auto-collectors
# ---------------------------------------------------------------------------
# Maps a metric name (as declared in fault.default_metrics) to a shell command
# that returns a single numeric value on stdout.  Metrics NOT listed here are
# expected to come from the workload's return dict instead.

_SYSTEM_CMDS: dict[str, str] = {
    # CPU
    "cpu_percent": (
        "top -bn1 2>/dev/null | grep -E '%Cpu|Cpu\\(s\\)' | head -1 | "
        "awk '{for(i=1;i<=NF;i++){if($i~/id/){printf \"%.1f\", 100-$(i-1); exit}}}'"
    ),
    # Memory
    "memory_mb":    "free -m 2>/dev/null | awk '/^Mem/{print $3}'",
    "swap_used_mb": "free -m 2>/dev/null | awk '/^Swap/{print $3}'",
    # Network
    "rtt_ms": (
        "ping -c 3 -W 2 127.0.0.1 2>/dev/null | awk -F'/' '/rtt|round-trip/{print $5}'"
    ),
    # Disk
    "disk_used_bytes": "df / 2>/dev/null | awk 'NR==2{printf \"%d\", $3*1024}'",
    "inode_used": (
        "df -i / 2>/dev/null | awk 'NR==2{print $3}'"
    ),
    # I/O
    "iops": (
        "iostat -d 1 1 2>/dev/null | awk '/^[a-zA-Z]/{t+=$4} END{printf \"%.0f\", t+0}'"
    ),
    # GPU (nvidia-smi)
    "gpu_util_percent": (
        "nvidia-smi --query-gpu=utilization.gpu "
        "--format=csv,noheader,nounits 2>/dev/null | head -1"
    ),
    "gpu_memory_mb": (
        "nvidia-smi --query-gpu=memory.used "
        "--format=csv,noheader,nounits 2>/dev/null | head -1"
    ),
    "gpu_clock_mhz": (
        "nvidia-smi --query-gpu=clocks.gr "
        "--format=csv,noheader,nounits 2>/dev/null | head -1"
    ),
}


def collect_system_snapshot(
    target: "Target",
    metric_names: list[str],
) -> dict[str, float]:
    """Run system commands for each auto-collectible metric in *metric_names*.

    Parameters
    ----------
    target : Target
        The target machine to collect metrics from.
    metric_names : list[str]
        Names of system metrics to try (only those in ``_SYSTEM_CMDS``
        are auto-collected; others return no value).

    Returns
    -------
    dict[str, float]
        ``{metric_name: value}`` for every metric that was successfully collected.
    """
    result: dict[str, float] = {}
    for name in metric_names:
        cmd = _SYSTEM_CMDS.get(name)
        if cmd is None:
            continue
        try:
            _, out, _ = target.run(cmd)
            raw = out.strip()
            if raw:
                result[name] = float(raw)
        except Exception:
            pass
    return result


def collect_recovery_samples(
    target: "Target",
    active_system: list[str],
    window_s: float,
    interval_s: float = 10.0,
) -> "list[MetricSample]":
    """Periodically collect system metrics for *window_s* seconds after fault stop.

    Parameters
    ----------
    target : Target
        The target machine to sample.
    active_system : list[str]
        System metric names to collect (must be in ``_SYSTEM_CMDS``).
    window_s : float
        Total duration of the recovery window in seconds.
    interval_s : float
        Seconds between samples. Default ``10.0``.

    Returns
    -------
    list[MetricSample]
        Ordered list of samples taken during the recovery window.
    """
    from chaos_jungle.metrics.schema import MetricSample  # avoid circular import

    samples: list[MetricSample] = []
    deadline = time.monotonic() + window_s
    trial = 0

    while time.monotonic() < deadline:
        values = collect_system_snapshot(target, active_system)
        if values:
            samples.append(MetricSample(
                timestamp_s=time.time(),
                phase="recovery",
                trial=trial,
                values=values,
            ))
        trial += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_s, remaining))

    return samples


# ---------------------------------------------------------------------------
# CollectStrategy
# ---------------------------------------------------------------------------


class CollectStrategy:
    """Defines how often metrics are sampled during a :meth:`~chaos_jungle.runner.ChaosRunner.measure` run.

    Two built-in strategies are provided as class attributes:

    ``CollectStrategy.SNAPSHOT``
        Collect at three fixed points: **before** the fault (baseline),
        **during** the fault, and **immediately after** the fault stops
        (one recovery snapshot).  Gives a fast before/after comparison
        with minimal overhead.

    ``CollectStrategy.RECOVERY``
        Like SNAPSHOT but continues sampling system metrics for
        ``recovery_window_s`` seconds after the fault stops, building a
        time-series view of how quickly the system returns to normal.
        Default window is 60 s; sample interval is 10 s.

    Parameters
    ----------
    mode : ``"snapshot"`` | ``"recovery"``
        Collection mode.
    recovery_window_s : float
        How long (seconds) to keep sampling after the fault stops in
        RECOVERY mode.  Default ``60.0``.
    recovery_interval_s : float
        Seconds between recovery samples.  Default ``10.0``.

    Examples
    --------
    Snapshot (3 data points)::

        result = runner.measure(
            workload,
            strategy=CollectStrategy.SNAPSHOT,
            metric_set=MetricSet.DEFAULT,
        )
        cm = result.collected_metrics
        print(cm.delta)           # fault.avg - baseline.avg per metric

    Recovery window (customised)::

        result = runner.measure(
            workload,
            strategy=CollectStrategy.RECOVERY(recovery_window_s=120),
            metric_set=MetricSet.DEFAULT.exclude("swap_used_mb"),
        )
        for s in cm.recovery["cpu_percent"].series:
            print(s.timestamp_s, s.values["cpu_percent"])
    """

    # Class-level singletons set after class definition
    SNAPSHOT: "CollectStrategy"
    RECOVERY: "CollectStrategy"

    def __init__(
        self,
        mode: str = "snapshot",
        recovery_window_s: float = 60.0,
        recovery_interval_s: float = 10.0,
    ) -> None:
        if mode not in ("snapshot", "recovery"):
            raise ValueError(
                f"CollectStrategy mode must be 'snapshot' or 'recovery', got {mode!r}."
            )
        self.mode = mode
        self.recovery_window_s = recovery_window_s
        self.recovery_interval_s = recovery_interval_s

    def __call__(
        self,
        recovery_window_s: float = 60.0,
        recovery_interval_s: float = 10.0,
    ) -> "CollectStrategy":
        """Allow ``CollectStrategy.RECOVERY(recovery_window_s=120)`` syntax."""
        return CollectStrategy(
            mode=self.mode,
            recovery_window_s=recovery_window_s,
            recovery_interval_s=recovery_interval_s,
        )

    def __repr__(self) -> str:
        if self.mode == "recovery":
            return (
                f"CollectStrategy.RECOVERY("
                f"recovery_window_s={self.recovery_window_s}, "
                f"recovery_interval_s={self.recovery_interval_s})"
            )
        return "CollectStrategy.SNAPSHOT"


CollectStrategy.SNAPSHOT = CollectStrategy("snapshot")
CollectStrategy.RECOVERY = CollectStrategy("recovery", recovery_window_s=60.0)
