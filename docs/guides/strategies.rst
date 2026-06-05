.. _guide-strategies:

Chaos Strategies
================

A *chaos strategy* controls **when** and **how often** faults fire.  Choosing
the right strategy for your experiment makes the difference between a test that
finds real bugs and one that just adds noise.

The table below lists every strategy, its implementation status in
chaos-jungle, and the typical use-case.

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Strategy
     - Status
     - When to use
   * - **Percentage-Based**
     - Implemented
     - Fire a fault on a random X % of requests — simulates a flaky dependency
   * - **Stochastic / Random**
     - Implemented
     - Same as percentage-based; a probability roll per request
   * - **Ramp-Up / Progressive**
     - Implemented
     - Increase fault probability over time — catches silent degradation
   * - **Event-Driven**
     - Planned
     - Trigger faults in response to system events (CPU spike, error rate, etc.)
   * - **Time-Window**
     - Planned
     - Active only during a specific wall-clock window (e.g. business hours)
   * - **Targeted / Selective**
     - Partial
     - Apply faults only to requests matching a URL or header pattern
   * - **Scheduled**
     - Partial
     - Run faults on a cron-like schedule
   * - **Cascading**
     - Partial
     - Chain faults so that one failure triggers another
   * - **Burst**
     - Planned
     - Fire many faults in a short spike, then return to normal
   * - **Canary**
     - Planned
     - Affect only a small percentage of traffic (like a canary deployment)
   * - **Correlated**
     - Planned
     - Faults that share state and fire together
   * - **Adaptive**
     - Planned
     - Adjust fault intensity based on observed system health
   * - **Manual / On-demand**
     - Implemented
     - Developer triggers faults explicitly via ``inject()`` or the runner
   * - **Cycling (door)**
     - Implemented
     - Alternate between fault-ON and fault-OFF phases N times

----

Percentage-Based / Stochastic
------------------------------

Pass a ``probability`` between ``0.0`` (never) and ``1.0`` (always) to any
:class:`~chaos_jungle.intercept.Behavior`.  The roll is made **once per
request** so ``before()`` and ``after()`` are always consistent.

.. code-block:: python

   from chaos_jungle.intercept import inject, Latency, Unavailable

   # 30 % of LLM calls get a 2-second delay
   with inject(Latency(2.0, probability=0.30)):
       run_pipeline()

   # 10 % of calls return 503
   with inject(Unavailable(probability=0.10)):
       run_pipeline()

Combine probability with stacking to model realistic degraded-service
scenarios:

.. code-block:: python

   # Every call gets 0.5 s delay; 20 % also get a 503
   with inject(Latency(0.5), Unavailable(probability=0.20)):
       run_pipeline()

All built-in behaviors accept the parameter:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Behavior
     - Probabilistic example
   * - ``Latency(s, probability=p)``
     - ``Latency(3.0, probability=0.25)`` — slow 25 % of calls
   * - ``Jitter(min_s, max_s, probability=p)``
     - ``Jitter(0.1, 1.5, probability=0.50)`` — random delay on half
   * - ``RateLimit(after_n, probability=p)``
     - ``RateLimit(after_n=5, probability=0.80)``
   * - ``Unavailable(probability=p)``
     - ``Unavailable(probability=0.05)`` — 5 % outage rate
   * - ``Timeout(probability=p)``
     - ``Timeout(probability=0.10)``
   * - ``CorruptResponse(probability=p)``
     - ``CorruptResponse(probability=0.15)``

Custom behaviors inherit the same mechanism — set ``self.probability`` in
``__init__`` and chaos-jungle handles the rest:

.. code-block:: python

   from chaos_jungle.intercept import Behavior, inject

   class SlowOnce(Behavior):
       def __init__(self, probability=0.5):
           self.probability = probability
           self._fired = False

       def before(self, url):
           if not self._fired:
               import time; time.sleep(2.0)
               self._fired = True

   with inject(SlowOnce(probability=0.3)):
       run_pipeline()

----

Ramp-Up / Progressive
-----------------------

Gradually increase fault pressure to find the *tipping point* where a system
starts to degrade.  Use a loop that raises ``probability`` (or delay duration)
on each iteration:

.. code-block:: python

   import time
   from chaos_jungle.intercept import inject, Latency

   steps = [0.0, 0.10, 0.25, 0.50, 0.75, 1.0]   # fault probability ramp
   for p in steps:
       print(f"--- probability={p:.0%} ---")
       with inject(Latency(2.0, probability=p)):
           result = run_pipeline()
           print(result)

Or ramp latency rather than probability:

.. code-block:: python

   for delay in [0.1, 0.5, 1.0, 2.0, 5.0]:
       print(f"--- delay={delay}s ---")
       with inject(Latency(delay)):
           run_pipeline()

With ``ChaosRunner`` and ``chaos_measure``:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import LLMLatency
   from chaos_jungle.targets import LocalTarget
   from chaos_jungle.decorators import chaos_measure
   from chaos_jungle.intercept import inject, Latency

   for p in [0.0, 0.25, 0.50, 1.0]:
       @chaos_measure(scenario_name=f"ramp-p{int(p*100)}")
       def experiment():
           with inject(Latency(3.0, probability=p)):
               return run_pipeline()
       summary = experiment()
       print(f"p={p:.0%}  duration={summary['duration_s']:.1f}s")

----

Cycling / door
--------------

:func:`~chaos_jungle.runner.ChaosRunner.door` alternates between a *fault-ON*
phase and a *rest* phase for ``cycles`` iterations.  It is useful for testing
whether a system recovers after a fault clears.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import LLMLatency
   from chaos_jungle.targets import LocalTarget

   runner = ChaosRunner(
       Scenario("cycling", faults=[LLMLatency("500ms")]),
       LocalTarget(),
   )

   results = runner.door(
       fault_duration=30,   # fault ON for 30 s
       rest_duration=30,    # fault OFF for 30 s
       cycles=3,            # repeat 3 times
       workload=run_pipeline,
   )

   for r in results:
       print(r["cycle"], r["phase"], r["metrics"])

``door()`` can also be used standalone with the intercept layer:

.. code-block:: python

   from chaos_jungle.intercept import door, Latency

   door(
       Latency(2.0),
       fault_duration="30s",
       rest_duration="30s",
       cycles=3,
       workload=run_pipeline,
   )

----

Manual / On-demand
------------------

The simplest strategy: the developer decides exactly when faults are active by
using ``inject()`` as a context manager or the ``@chaos`` decorator.

.. code-block:: python

   from chaos_jungle.intercept import inject, RateLimit
   from chaos_jungle.decorators import chaos
   from chaos_jungle.faults import NetworkDelay

   # Context manager — explicit start and end
   with inject(RateLimit(after_n=5)):
       run_experiment()

   # Decorator — wraps the entire function
   @chaos(NetworkDelay("200ms"))
   def run_experiment():
       ...

----

Targeted / Selective
---------------------

Restrict faults to specific providers or URLs using the ``urls`` parameter of
``inject()``:

.. code-block:: python

   from chaos_jungle.intercept import inject, Unavailable

   # Only OpenAI calls fail
   with inject(Unavailable(), urls=["api.openai.com"]):
       run_pipeline()

   # Only local Ollama calls are slowed
   with inject(Latency(1.5), urls=["localhost", "127.0.0.1"]):
       run_pipeline()

See :ref:`guide-intercept` for the full URL filtering reference.

----

See also
--------

* :ref:`guide-intercept` — ``inject()``, ``door()``, and all built-in behaviors
* :ref:`guide-llm` — proxy-based LLM API faults (``LLMLatency``, ``LLMRateLimit``, …)
* :ref:`guide-measurement` — ``runner.measure()`` and quality gates
