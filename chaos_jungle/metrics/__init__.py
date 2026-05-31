"""Metric interface and built-in collectors for chaos-jungle.

Use metrics with :func:`~chaos_jungle.decorators.chaos_measure` to
automatically measure baseline and chaos-phase values without writing
any boilerplate:

.. code-block:: python

    from chaos_jungle.decorators import chaos_measure
    from chaos_jungle.faults import NetworkDelay
    from chaos_jungle.metrics import PingLatency, CommandMetric

    @chaos_measure(
        NetworkDelay("100ms"),
        metrics=[PingLatency("8.8.8.8", count=5)],
    )
    def run_experiment():
        run_pipeline()
        return {"retries": 3}

    summary = run_experiment()
    # summary["fn_result"]              → {"retries": 3}
    # summary["metrics"]["baseline"]    → {"ping_avg_ms": 0.3, ...}
    # summary["metrics"]["chaos"]       → {"ping_avg_ms": 108.6, ...}
"""

from chaos_jungle.metrics.base import Metric
from chaos_jungle.metrics.builtin import PingLatency, CommandMetric, FileIntegrity

__all__ = [
    "Metric",
    "PingLatency",
    "CommandMetric",
    "FileIntegrity",
]
