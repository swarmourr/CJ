"""Datadog DogStatsD exporter.

Sends metrics via DogStatsD to a local Datadog Agent (default port 8125).
No API key required — the agent handles forwarding.

Requires ``datadog``::

    pip install datadog

Example::

    from chaos_jungle.exporters import DatadogExporter

    exporter = DatadogExporter(
        host="localhost",
        port=8125,
        tags=["env:staging", "team:platform"],
    )
    result = runner.measure(workload, n_baseline=5, n_fault=5)
    exporter.export(result)

Metrics sent (all as gauges with ``scenario`` and any custom tags):

* ``chaos_jungle.baseline.{metric}``
* ``chaos_jungle.fault.{metric}``
* ``chaos_jungle.delta.{metric}``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chaos_jungle.exporters.base import Exporter

if TYPE_CHECKING:
    from chaos_jungle.runner import MeasurementResult


class DatadogExporter(Exporter):
    """Send MeasurementResult metrics to Datadog via DogStatsD.

    Parameters
    ----------
    host : str
        DogStatsD host. Default ``"localhost"``.
    port : int
        DogStatsD port. Default ``8125``.
    tags : list[str], optional
        Extra tags applied to every metric, e.g. ``["env:prod", "region:us-east"]``.
    namespace : str
        Metric namespace prefix. Default ``"chaos_jungle"``.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8125,
        tags: list[str] | None = None,
        namespace: str = "chaos_jungle",
    ) -> None:
        self.host = host
        self.port = port
        self.tags = tags or []
        self.namespace = namespace

    def export(self, result: "MeasurementResult") -> None:
        try:
            from datadog import initialize, statsd
        except ImportError:
            raise ImportError(
                "datadog is required for DatadogExporter: pip install datadog"
            )

        initialize(statsd_host=self.host, statsd_port=self.port)

        base_tags = [f"scenario:{result.scenario}"] + self.tags

        def _send(phase: str, metrics: dict) -> None:
            for name, value in metrics.items():
                if not isinstance(value, (int, float)):
                    continue
                safe_name = name.replace("-", "_")
                statsd.gauge(
                    f"{self.namespace}.{phase}.{safe_name}",
                    value,
                    tags=base_tags,
                )

        _send("baseline", result.baseline)
        _send("fault", result.fault)
        _send("delta", result.delta)

        print(
            f"[chaos-jungle] DatadogExporter: sent {result.scenario!r} metrics "
            f"to {self.host}:{self.port}"
        )
