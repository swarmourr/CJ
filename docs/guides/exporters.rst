.. _guide-exporters:

Observability Exporters
=======================

Exporters push :class:`~chaos_jungle.runner.MeasurementResult` metrics to
external observability systems after an experiment completes.  Without
exporters, results live only in the local SQLite database.  Exporters let
you correlate fault injection events with your existing dashboards, alerts,
and SLO tracking.

Three exporters are built in:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Exporter
     - Destination
   * - :class:`~chaos_jungle.exporters.PrometheusExporter`
     - Prometheus Pushgateway
   * - :class:`~chaos_jungle.exporters.DatadogExporter`
     - Datadog via DogStatsD (local agent)
   * - :class:`~chaos_jungle.exporters.WebhookExporter`
     - Any HTTP endpoint (no dependencies)

All exporters share the same interface::

    exporter.export(result)

----

PrometheusExporter
------------------

Pushes metrics to a `Prometheus Pushgateway
<https://prometheus.io/docs/practices/pushing/>`_.

**Install**::

    pip install prometheus-client

**Usage**

.. code-block:: python

   from chaos_jungle.exporters import PrometheusExporter

   exporter = PrometheusExporter(
       gateway="http://pushgateway:9091",
       job="chaos_jungle",
       grouping_key={"env": "staging"},
   )
   result = runner.measure(workload, n_baseline=5, n_fault=5)
   exporter.export(result)

**Metrics pushed** (all with ``scenario`` label):

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Metric name
     - Value
   * - ``cj_baseline_{metric}``
     - Average baseline value
   * - ``cj_fault_{metric}``
     - Average fault value
   * - ``cj_delta_{metric}``
     - ``fault - baseline`` delta

**Parameters**

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Parameter
     - Default
     - Description
   * - ``gateway``
     - required
     - Pushgateway URL, e.g. ``"http://pushgateway:9091"``
   * - ``job``
     - ``"chaos_jungle"``
     - Prometheus job label
   * - ``grouping_key``
     - ``{}``
     - Extra labels added to every metric
   * - ``timeout_s``
     - ``10``
     - HTTP request timeout in seconds

----

DatadogExporter
---------------

Sends metrics via DogStatsD to a running Datadog Agent.  No API key required.

**Install**::

    pip install datadog

**Usage**

.. code-block:: python

   from chaos_jungle.exporters import DatadogExporter

   exporter = DatadogExporter(
       host="localhost",
       port=8125,
       tags=["env:staging", "team:platform"],
   )
   result = runner.measure(workload, n_baseline=5, n_fault=5)
   exporter.export(result)

**Metrics sent** (all as gauges with ``scenario`` and custom tags):

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Metric name
     - Value
   * - ``chaos_jungle.baseline.{metric}``
     - Average baseline value
   * - ``chaos_jungle.fault.{metric}``
     - Average fault value
   * - ``chaos_jungle.delta.{metric}``
     - ``fault - baseline`` delta

**Parameters**

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Parameter
     - Default
     - Description
   * - ``host``
     - ``"localhost"``
     - DogStatsD host
   * - ``port``
     - ``8125``
     - DogStatsD port
   * - ``tags``
     - ``[]``
     - Extra tags applied to every metric
   * - ``namespace``
     - ``"chaos_jungle"``
     - Metric namespace prefix

----

WebhookExporter
---------------

POSTs the result as JSON to any HTTP endpoint.  No extra dependencies — uses
the Python standard library only.

**Usage**

.. code-block:: python

   from chaos_jungle.exporters import WebhookExporter

   exporter = WebhookExporter(
       url="https://hooks.example.com/chaos",
       headers={"Authorization": "Bearer my-token"},
   )
   result = runner.measure(workload, n_baseline=5, n_fault=5)
   exporter.export(result)

**Payload**

.. code-block:: json

   {
       "scenario":   "wan-degraded",
       "session_id": 42,
       "n_baseline": 5,
       "n_fault":    5,
       "baseline":   {"duration_s": 0.12},
       "fault":      {"duration_s": 0.34},
       "delta":      {"duration_s": 0.22},
       "hypothesis": {
           "name":   "handles delay gracefully",
           "passed": true,
           "assertions": [...]
       }
   }

The ``hypothesis`` key is included only when a
:class:`~chaos_jungle.hypothesis.Hypothesis` was passed to
``runner.measure()``.

**Parameters**

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Parameter
     - Default
     - Description
   * - ``url``
     - required
     - Destination URL
   * - ``headers``
     - ``{}``
     - Extra HTTP headers
   * - ``timeout_s``
     - ``10``
     - Request timeout in seconds
   * - ``raise_on_error``
     - ``True``
     - Raise ``RuntimeError`` on non-2xx response

----

Using multiple exporters
------------------------

Call ``export()`` on each exporter after a run:

.. code-block:: python

   from chaos_jungle.exporters import PrometheusExporter, WebhookExporter

   exporters = [
       PrometheusExporter(gateway="http://pushgateway:9091"),
       WebhookExporter(url="https://hooks.example.com/chaos"),
   ]

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   for exp in exporters:
       exp.export(result)

----

Custom exporter
---------------

Subclass :class:`~chaos_jungle.exporters.Exporter` to push to any system:

.. code-block:: python

   from chaos_jungle.exporters import Exporter

   class SlackExporter(Exporter):
       def __init__(self, webhook_url):
           self.webhook_url = webhook_url

       def export(self, result):
           import urllib.request, json
           text = (
               f"*Chaos experiment: {result.scenario}*\n"
               + "\n".join(
                   f"  {k}: baseline={v}  fault={result.fault.get(k)}  delta={result.delta.get(k)}"
                   for k, v in result.baseline.items()
                   if isinstance(v, (int, float))
               )
           )
           body = json.dumps({"text": text}).encode()
           req = urllib.request.Request(self.webhook_url, data=body)
           req.add_header("Content-Type", "application/json")
           urllib.request.urlopen(req, timeout=10)

----

See also
--------

* :ref:`guide-measurement` — ``ChaosRunner.measure()`` and ``MeasurementResult``
* :ref:`guide-hypothesis` — declare pass/fail criteria before running
* :ref:`guide-scheduler` — export results from scheduled runs automatically
