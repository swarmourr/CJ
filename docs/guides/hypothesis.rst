.. _guide-hypothesis:

Hypothesis
==========

A **Hypothesis** is a named set of metric assertions declared *before* an
experiment runs and verified *after*.  It answers the question:

    *"What must remain true for my system to be considered resilient?"*

Without a hypothesis you discover after the fact that something degraded.
With a hypothesis you state the acceptable boundary upfront, get a clear
pass/fail, and can use it as a CI gate.

----

Quick start
-----------

.. code-block:: python

   from chaos_jungle import Hypothesis, ChaosRunner, Scenario, NetworkDelay
   from chaos_jungle.targets import LocalTarget

   h = (
       Hypothesis("handles 200ms delay gracefully")
       .max_delta_pct("duration_s", 50)        # latency may grow by at most 50%
       .max_fault_value("error_rate", 0.05)    # error rate must stay under 5%
       .no_regression("completion_rate")        # completion rate must not drop
   )

   runner = ChaosRunner(
       Scenario("delay", [NetworkDelay("200ms")]),
       LocalTarget(),
   )
   result = runner.measure(workload, n_baseline=5, n_fault=5, hypothesis=h)
   print(result.hypothesis_result.summary())

Output::

   Hypothesis [PASS]: handles 200ms delay gracefully
     [PASS] duration_s                      delta=38.2% (limit=50%)
     [PASS] error_rate                      fault=0.02 (limit=0.05)
     [PASS] completion_rate                 fault=0.98 baseline=1.0 tolerance=0.0
     All 3 assertion(s) passed

----

Assertion methods
-----------------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Method
     - When it passes
   * - ``.max_delta_pct(metric, pct)``
     - Fault value increased by at most ``pct`` % relative to baseline
   * - ``.max_fault_value(metric, value)``
     - Fault value is <= ``value`` in absolute terms
   * - ``.min_fault_value(metric, value)``
     - Fault value is >= ``value`` in absolute terms
   * - ``.no_regression(metric, tolerance=0)``
     - Fault value >= ``baseline - tolerance`` (for metrics where lower is worse)
   * - ``.max_absolute_delta(metric, delta)``
     - ``|fault - baseline|`` <= ``delta``

All assertion methods return ``self`` so they can be chained.

----

Checking manually
-----------------

You can call ``.check()`` on any :class:`~chaos_jungle.runner.MeasurementResult`
without passing ``hypothesis=`` to ``runner.measure()``:

.. code-block:: python

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   h_result = h.check(result)

   if not h_result.passed:
       for a in h_result.assertions:
           if not a.passed:
               print(f"FAIL {a.metric}: {a.reason}")

----

Using as a CI gate
------------------

.. code-block:: python

   import pytest
   from chaos_jungle import Hypothesis, ChaosRunner, Scenario, LLMLatency
   from chaos_jungle.targets import LocalTarget

   def test_agent_resilience():
       h = (
           Hypothesis("agent survives slow LLM")
           .max_delta_pct("duration_s", 200)
           .max_fault_value("error_rate", 0.1)
       )

       runner = ChaosRunner(
           Scenario("slow-llm", [LLMLatency(delay_s=3.0)]),
           LocalTarget(),
       )
       result = runner.measure(workload, n_baseline=3, n_fault=3, hypothesis=h)

       assert result.hypothesis_result.passed, (
           result.hypothesis_result.summary()
       )

----

HypothesisResult fields
------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Field
     - Description
   * - ``passed``
     - ``True`` if all assertions passed
   * - ``name``
     - The hypothesis name
   * - ``assertions``
     - List of :class:`~chaos_jungle.hypothesis.AssertionResult` — one per assertion
   * - ``.summary()``
     - Human-readable pass/fail table

Each :class:`~chaos_jungle.hypothesis.AssertionResult` has: ``metric``,
``kind``, ``passed``, ``expected``, ``actual``, ``reason``.

----

See also
--------

* :ref:`guide-measurement` — ``ChaosRunner.measure()`` and ``MeasurementResult``
* :ref:`guide-oracles` — assertion on raw workload outputs (no measurement required)
* :ref:`guide-fuzzing` — explore fault combinations and surface hypothesis failures
