Metrics — measuring chaos impact
=================================

chaos-jungle provides a ``Metric`` interface for defining *what to measure*
and *how to measure it*. Pass a list of metrics to
:func:`~chaos_jungle.decorators.chaos_measure` and the framework:

1. Collects **baseline** values *before* chaos starts.
2. Collects **chaos** values *after* the function returns (while chaos is still
   active, before revert).
3. Merges everything into one result record in the database and the dashboard.

----

Built-in metrics
-----------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Class
     - Parameters
     - Returns
   * - ``PingLatency``
     - ``host``, ``count=5``
     - ``avg_ms``, ``min_ms``, ``max_ms``, ``samples``
   * - ``CommandMetric``
     - ``cmd``, ``parse``, ``name``
     - whatever your ``parse`` fn returns
   * - ``FileIntegrity``
     - ``pattern``, ``directory``, ``checksum_file=None``
     - ``files_found``, ``files_corrupted``
   * - ``ThroughputMetric``
     - ``url``
     - ``speed_mbps``, ``time_s``

----

Quick example
--------------

.. code-block:: python

   from chaos_jungle.decorators import chaos_measure
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.metrics import PingLatency, CommandMetric

   @chaos_measure(
       NetworkDelay("100ms", jitter="10ms"),
       metrics=[
           PingLatency("8.8.8.8", count=5),
           CommandMetric(
               "ss -tn state established | wc -l",
               parse=lambda out: {"open_connections": int(out.strip())},
               name="tcp",
           ),
       ],
       scenario_name="network-impact",
   )
   def run_experiment():
       run_pipeline()
       return {"jobs_completed": 42}

   summary = run_experiment()

   baseline = summary["metrics"]["baseline"]
   chaos    = summary["metrics"]["chaos"]

   print(f"Latency:     {baseline['ping_avg_ms']} ms  →  {chaos['ping_avg_ms']} ms")
   print(f"Connections: {baseline['tcp_open_connections']}  →  {chaos['tcp_open_connections']}")

The result stored in the database (and CSV export) looks like:

.. code-block:: python

   {
       "baseline_ping_avg_ms":         0.2,
       "baseline_ping_min_ms":         0.1,
       "baseline_ping_max_ms":         0.4,
       "baseline_ping_samples":        5,
       "baseline_tcp_open_connections": 12,
       "chaos_ping_avg_ms":          108.6,
       "chaos_ping_min_ms":          100.1,
       "chaos_ping_max_ms":          115.3,
       "chaos_ping_samples":           5,
       "chaos_tcp_open_connections":   8,
       "fn_jobs_completed":            42,
   }

----

Custom metrics
---------------

Subclass :class:`~chaos_jungle.metrics.Metric` and implement
:meth:`~chaos_jungle.metrics.Metric.collect`:

.. code-block:: python

   from chaos_jungle.metrics import Metric
   from chaos_jungle.targets.base import Target

   class OpenConnections(Metric):
       """Count established TCP connections on the target."""
       name = "tcp"

       def collect(self, target: Target) -> dict:
           _, out, _ = target.run("ss -tn state established | wc -l")
           try:
               return {"open_connections": int(out.strip())}
           except ValueError:
               return {"open_connections": 0}


   class RetransmitRate(Metric):
       """Parse TCP retransmit count from /proc/net/snmp."""
       name = "tcp_retrans"

       def collect(self, target: Target) -> dict:
           _, out, _ = target.run(
               "awk '/^Tcp:/{getline; print $12}' /proc/net/snmp"
           )
           try:
               return {"retransmits": int(out.strip())}
           except ValueError:
               return {"retransmits": 0}

Use them exactly like built-ins:

.. code-block:: python

   @chaos_measure(
       NetworkDelay("100ms"),
       metrics=[OpenConnections(), RetransmitRate()],
   )
   def run():
       run_pipeline()

----

Metrics on remote targets
--------------------------

All ``collect(target)`` calls receive the same target the runner uses.
For an ``SSHTarget``, ``target.run(cmd)`` executes on the remote machine —
so metrics measure the *remote* host automatically, no extra setup needed:

.. code-block:: python

   from chaos_jungle.targets import SSHTarget
   from chaos_jungle.metrics import PingLatency, FileIntegrity

   target = SSHTarget("worker1", user="ubuntu")

   @chaos_measure(
       NetworkDelay("100ms"),
       target=target,
       metrics=[
           PingLatency("storage-node", count=5),
           FileIntegrity("*.pdb", "/scratch/data",
                         checksum_file="/scratch/ref.md5"),
       ],
   )
   def run_remote():
       run_pipeline()

----

Standalone metric collection
------------------------------

You can also call metrics directly outside of a decorator:

.. code-block:: python

   from chaos_jungle.metrics import PingLatency
   from chaos_jungle.targets import LocalTarget

   m = PingLatency("8.8.8.8", count=3)
   result = m.collect(LocalTarget())
   print(result)  # {"avg_ms": 12.3, "min_ms": 10.1, "max_ms": 14.7, "samples": 3}
