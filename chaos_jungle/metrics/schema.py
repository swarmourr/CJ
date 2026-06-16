"""Data structures for collected metrics results."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class MetricSample:
    """A single observation of one or more metrics at a point in time.

    Attributes
    ----------
    timestamp_s : float
        Unix timestamp when the sample was taken.
    phase : str
        ``"baseline"``, ``"fault"``, or ``"recovery"``.
    trial : int
        Trial index within the phase (0-based).
    values : dict[str, float]
        Metric name → observed value.
    """

    timestamp_s: float
    phase: str
    trial: int
    values: dict[str, float]


@dataclass
class MetricSummary:
    """Aggregated statistics for one metric across multiple samples.

    Attributes
    ----------
    avg : float
    min : float
    max : float
    p50 : float
        Median value.
    p99 : float
        99th-percentile value.
    series : list[MetricSample]
        Raw samples used to compute the summary (kept for time-series plots).
    """

    avg: float
    min: float
    max: float
    p50: float
    p99: float
    series: list[MetricSample] = field(default_factory=list, repr=False)

    @classmethod
    def from_values(
        cls,
        vals: list[float],
        samples: list[MetricSample] | None = None,
    ) -> "MetricSummary":
        """Build a summary from a flat list of float values."""
        if not vals:
            return cls(avg=0.0, min=0.0, max=0.0, p50=0.0, p99=0.0, series=samples or [])
        sorted_v = sorted(vals)
        n = len(sorted_v)
        return cls(
            avg=round(statistics.mean(sorted_v), 6),
            min=round(sorted_v[0], 6),
            max=round(sorted_v[-1], 6),
            p50=round(sorted_v[n // 2], 6),
            p99=round(sorted_v[min(int(n * 0.99), n - 1)], 6),
            series=samples or [],
        )


@dataclass
class CollectedMetrics:
    """All metric data gathered during a :meth:`~chaos_jungle.runner.ChaosRunner.measure` run.

    Attributes
    ----------
    strategy : str
        Name of the strategy used (``"snapshot"`` or ``"recovery"``).
    active_metrics : list[str]
        Metric names that were collected (after MetricSet filtering).
    baseline : dict[str, MetricSummary]
        Metric summaries from baseline phase (no fault).
    fault : dict[str, MetricSummary]
        Metric summaries from fault phase.
    recovery : dict[str, MetricSummary]
        Metric summaries from post-fault recovery window.
        Empty for ``"snapshot"`` strategy.
    delta : dict[str, float]
        ``fault.avg - baseline.avg`` for each numeric metric.
        Positive = fault made the metric worse (higher).
    """

    strategy: str
    active_metrics: list[str]
    baseline: dict[str, MetricSummary] = field(default_factory=dict)
    fault: dict[str, MetricSummary] = field(default_factory=dict)
    recovery: dict[str, MetricSummary] = field(default_factory=dict)
    delta: dict[str, float] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        strategy: str,
        active_metrics: list[str],
        baseline_samples: list[MetricSample],
        fault_samples: list[MetricSample],
        recovery_samples: list[MetricSample],
    ) -> "CollectedMetrics":
        """Aggregate raw samples into summaries and compute deltas."""

        def _summarise(samples: list[MetricSample], name: str) -> MetricSummary | None:
            vals = [
                s.values[name]
                for s in samples
                if name in s.values and s.values[name] is not None
            ]
            if not vals:
                return None
            return MetricSummary.from_values(vals, samples=[s for s in samples if name in s.values])

        baseline: dict[str, MetricSummary] = {}
        fault:    dict[str, MetricSummary] = {}
        recovery: dict[str, MetricSummary] = {}
        delta:    dict[str, float] = {}

        for name in active_metrics:
            b = _summarise(baseline_samples, name)
            f = _summarise(fault_samples,    name)
            r = _summarise(recovery_samples, name)
            if b:
                baseline[name] = b
            if f:
                fault[name] = f
            if r:
                recovery[name] = r
            if b and f:
                delta[name] = round(f.avg - b.avg, 6)

        return cls(
            strategy=strategy,
            active_metrics=active_metrics,
            baseline=baseline,
            fault=fault,
            recovery=recovery,
            delta=delta,
        )
