.. _guide-scheduler:

Scheduler
=========

``ChaosScheduler`` runs a chaos experiment on a recurring schedule in a
background thread.  It turns chaos from a one-shot operation into a continuous
practice — experiments run automatically without any manual trigger.

----

Quick start
-----------

.. code-block:: python

   from chaos_jungle import ChaosScheduler, Scenario, NetworkDelay
   from chaos_jungle.targets import SSHTarget

   scenario = Scenario("nightly-chaos", [NetworkDelay("200ms")])
   target   = SSHTarget("worker1", user="ubuntu")

   def workload():
       import time
       t0 = time.time()
       call_my_service()
       return {"duration_s": round(time.time() - t0, 2)}

   scheduler = (
       ChaosScheduler(scenario, target, workload=workload)
       .every("1h")                            # run every hour
       .on_result(lambda r: print(r.summary())) # called after each run
   )
   scheduler.start()

   # ... your application runs ...
   scheduler.stop()

----

Schedule modes
--------------

**Interval** — run every N seconds / minutes / hours:

.. code-block:: python

   scheduler.every("30m")    # every 30 minutes
   scheduler.every("1h")     # every hour
   scheduler.every("90s")    # every 90 seconds
   scheduler.every(3600)     # plain seconds also accepted

**Daily at a fixed time** — run once per day at a specific time:

.. code-block:: python

   scheduler.daily_at("02:00")   # every night at 2 AM
   scheduler.daily_at("14:30")   # every day at 2:30 PM

Only one schedule mode is active at a time.  The last call to ``.every()`` or
``.daily_at()`` wins.

----

Callbacks
---------

**On result** — called with each :class:`~chaos_jungle.runner.MeasurementResult`
after a successful run:

.. code-block:: python

   from chaos_jungle.exporters import WebhookExporter

   exporter = WebhookExporter(url="https://hooks.example.com/chaos")

   scheduler = (
       ChaosScheduler(scenario, target, workload=workload)
       .every("1h")
       .on_result(exporter.export)       # push to webhook after each run
       .on_result(lambda r: print(r.summary()))
   )

Multiple callbacks can be registered.  They are called in registration order.

**On error** — called when a scheduled run raises an exception:

.. code-block:: python

   def handle_error(exc):
       send_alert(f"Chaos run failed: {exc}")

   scheduler.on_error(handle_error)

----

Accessing results
-----------------

All :class:`~chaos_jungle.runner.MeasurementResult` objects collected so far
are available on ``scheduler.results``:

.. code-block:: python

   scheduler.stop()
   for r in scheduler.results:
       print(r.scenario, r.delta)

----

Without a workload
------------------

If no ``workload`` is provided, the scheduler runs ``start()`` / ``stop()``
only.  The fault is injected for the fault duration, then reverted.  No
measurement is performed and no result is stored.

.. code-block:: python

   # Inject chaos every night — no measurement
   scheduler = (
       ChaosScheduler(scenario, target)
       .daily_at("02:00")
   )
   scheduler.start()

----

Parameters
----------

.. code-block:: python

   ChaosScheduler(
       scenario,            # Scenario — the scenario to run
       target=None,         # Target  — defaults to LocalTarget()
       workload=None,       # Callable[[], dict] — passed to runner.measure()
       n_baseline=1,        # int — baseline trials per scheduled run
       n_fault=1,           # int — fault trials per scheduled run
   )

   scheduler.every(interval)       # str | int | float — interval between runs
   scheduler.daily_at(time_str)    # str — "HH:MM" time of day

   scheduler.on_result(callback)   # Callable[[MeasurementResult], None]
   scheduler.on_error(callback)    # Callable[[Exception], None]

   scheduler.start()               # start the background thread
   scheduler.stop()                # stop gracefully
   scheduler.results               # list[MeasurementResult] collected so far

----

See also
--------

* :ref:`guide-measurement` — ``ChaosRunner.measure()`` and ``MeasurementResult``
* :ref:`guide-exporters` — push results to Prometheus, Datadog, or a webhook
* :ref:`guide-hypothesis` — declare pass/fail criteria to gate scheduled runs
