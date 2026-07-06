"""Prometheus Pushgateway exporter.

Requires ``prometheus-client``::

    pip install prometheus-client

Example::

    from chaos_jungle.exporters import PrometheusExporter

    exporter = PrometheusExporter(
        gateway="http://pushgateway:9091",
        job="chaos_jungle",
    )
    result = runner.measure(workload, n_baseline=5, n_fault=5)
    exporter.export(result)

Metrics pushed (all with ``scenario`` label):

* ``cj_baseline_{metric}``  — average baseline value
* ``cj_fault_{metric}``     — average fault value
* ``cj_delta_{metric}``     — fault - baseline delta
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chaos_jungle.exporters.base import Exporter

if TYPE_CHECKING:
    from chaos_jungle.runner import MeasurementResult


class PrometheusExporter(Exporter):
    """Push MeasurementResult metrics to a Prometheus Pushgateway.

    Parameters
    ----------
    gateway : str
        URL of the Pushgateway, e.g. ``"http://pushgateway:9091"``.
    job : str
        Prometheus job label. Default ``"chaos_jungle"``.
    grouping_key : dict, optional
        Extra labels added to every metric, e.g. ``{"env": "staging"}``.
    timeout_s : float
        HTTP request timeout in seconds. Default ``10``.
    """

    def __init__(
        self,
        gateway: str,
        job: str = "chaos_jungle",
        grouping_key: dict | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.gateway = gateway
        self.job = job
        self.grouping_key = grouping_key or {}
        self.timeout_s = timeout_s

    def export(self, result: "MeasurementResult") -> None:
        try:
            from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
        except ImportError:
            raise ImportError(
                "prometheus-client is required for PrometheusExporter: "
                "pip install prometheus-client"
            )

        registry = CollectorRegistry()
        scenario = result.scenario

        def _push_phase(phase: str, metrics: dict) -> None:
            for name, value in metrics.items():
                if not isinstance(value, (int, float)):
                    continue
                safe_name = name.replace("-", "_").replace(".", "_")
                g = Gauge(
                    f"cj_{phase}_{safe_name}",
                    f"chaos-jungle {phase} {name}",
                    ["scenario"],
                    registry=registry,
                )
                g.labels(scenario=scenario).set(value)

        _push_phase("baseline", result.baseline)
        _push_phase("fault", result.fault)
        _push_phase("delta", result.delta)

        push_to_gateway(
            self.gateway,
            job=self.job,
            registry=registry,
            grouping_key=self.grouping_key or None,
            timeout=self.timeout_s,
        )
        print(f"[chaos-jungle] PrometheusExporter: pushed {scenario!r} to {self.gateway}")
