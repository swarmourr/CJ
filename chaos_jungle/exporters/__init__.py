"""Observability exporters — push MeasurementResult metrics to external systems.

Available exporters:

* :class:`PrometheusExporter` — push to a Prometheus Pushgateway
* :class:`DatadogExporter`   — push via DogStatsD (local agent)
* :class:`WebhookExporter`   — POST JSON to any HTTP endpoint

All exporters share the same interface::

    exporter.export(result)

Example::

    from chaos_jungle.exporters import PrometheusExporter, WebhookExporter

    result = runner.measure(workload, n_baseline=5, n_fault=5)

    PrometheusExporter(gateway="http://pushgateway:9091").export(result)
    WebhookExporter(url="https://hooks.example.com/chaos").export(result)
"""

from chaos_jungle.exporters.base import Exporter
from chaos_jungle.exporters.prometheus import PrometheusExporter
from chaos_jungle.exporters.datadog import DatadogExporter
from chaos_jungle.exporters.webhook import WebhookExporter

__all__ = ["Exporter", "PrometheusExporter", "DatadogExporter", "WebhookExporter"]
