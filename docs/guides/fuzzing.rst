.. _guide-fuzzing:

Fault Fuzzing
=============

``ChaosFuzzer`` explores the fault space automatically — it picks faults
randomly, injects them, and measures each one against a **shared baseline**.
Instead of specifying every scenario by hand, you describe a source of faults
and let the fuzzer discover which combinations cause failures.

``ChaosFuzzer`` follows the same pattern as ``ChaosRunner``: construct with a
source and a target, then call ``.measure()``.

----

Two modes
---------

**Explicit pool** — you provide the faults, CJ picks random subsets:

.. code-block:: python

   from chaos_jungle.fuzzing import ChaosFuzzer, summarise_fuzz
   from chaos_jungle.faults.llm import LLMLatency, LLMRateLimit, LLMUnavailable
   from chaos_jungle.intercept import ToolMutate
   from chaos_jungle import LocalTarget

   fuzzer = ChaosFuzzer(
       fault_pool=[
           LLMLatency(delay_s=3.0),
           LLMRateLimit(n=2),
           LLMUnavailable(),
           ToolMutate(mode="wrong_type"),
       ],
       target=LocalTarget(),
       seed=42,
   )
   results = fuzzer.measure(my_agent_fn, n_baseline=3, n_fault=3, n=15)
   print(summarise_fuzz(results))

**Category-based** — CJ picks the faults *and* randomizes their parameters:

.. code-block:: python

   fuzzer = ChaosFuzzer(
       categories=["llm", "system"],
       target=LocalTarget(),
   )
   results = fuzzer.measure(my_agent_fn, n=15)

Available categories: ``"system"`` (network, CPU, memory),
``"llm"`` (latency, rate limit, timeout, corrupt response, unavailable),
``"application"`` (skill file faults).

----

Shared baseline
---------------

``ChaosFuzzer`` measures a **single baseline** before any fault experiments
start.  Every :class:`~chaos_jungle.runner.MeasurementResult` in the returned
list carries that same baseline — all fault comparisons are consistent and no
redundant baseline runs are wasted.

.. code-block:: text

   shared baseline (n_baseline trials, no fault)
       ↓
   experiment 1 — random fault A      → MeasurementResult (baseline + fault A)
   experiment 2 — random fault B+C    → MeasurementResult (baseline + fault B+C)
   ...
   experiment N

----

Parameters
----------

.. code-block:: python

   ChaosFuzzer(
       fault_pool=None,         # list[Fault] — explicit pool (mutually exclusive with categories)
       categories=None,         # list[str]   — category names (mutually exclusive with fault_pool)
       target=None,             # Target — defaults to LocalTarget()
       seed=None,               # int | None — set for reproducible runs
       max_faults_per_run=2,    # max faults active simultaneously per experiment
       exclude=None,            # list[str] — fault class names to skip
   )

   fuzzer.measure(
       workload,                # Callable[[], dict] — same contract as ChaosRunner.measure()
       n_baseline=3,            # shared baseline trials
       n_fault=3,               # fault trials per experiment
       n=10,                    # number of random experiments
       stop_on_first_failure=False,
   )

----

Output
------

``summarise_fuzz()`` prints a tabular summary::

   Scenario                                          Pass  Fail      Cost   AvgLat
   ------------------------------------------------------------------------------
   fuzz/LLMLatency+ToolMutate                           5     0  $0.00012   3.24s
   fuzz/LLMRateLimit                                    4     1  $0.00008   1.12s
   fuzz/LLMUnavailable                                  2     3  $0.00000   0.01s
   ...
   15 combinations  —  3 caused oracle failures

----

CI — fail fast on first failure
---------------------------------

.. code-block:: python

   import pytest

   def test_no_oracle_failures_under_random_faults():
       fuzzer = ChaosFuzzer(
           fault_pool=[LLMLatency(3.0), LLMRateLimit(n=1), ToolMutate()],
           target=LocalTarget(),
           seed=0,
       )
       results = fuzzer.measure(
           my_agent_fn,
           n_baseline=1,
           n_fault=1,
           n=10,
           stop_on_first_failure=True,
       )
       failures = [r for r in results if not r.passed_all_oracles]
       assert not failures, (
           f"{len(failures)} fault combinations caused oracle failures: "
           + ", ".join(r.scenario for r in failures)
       )

----

Reproducing a specific combination
------------------------------------

Fix the seed to reproduce the same fault sequence, then isolate with
``ChaosRunner``:

.. code-block:: python

   # Same seed → same random sequence
   fuzzer = ChaosFuzzer(fault_pool=pool, target=target, seed=42)
   results = fuzzer.measure(workload, n=20)

   # Isolate the failing scenario
   from chaos_jungle import Scenario, ChaosRunner

   runner = ChaosRunner(
       Scenario("repro", [LLMRateLimit(n=2), ToolMutate()]),
       LocalTarget(),
   )
   result = runner.measure(workload, n_baseline=3, n_fault=3)
   print(result.summary())

----

Backward compatibility
----------------------

``fuzz_scenarios()`` is a thin wrapper around ``ChaosFuzzer`` and continues
to work unchanged.  New code should use ``ChaosFuzzer`` directly.

.. code-block:: python

   # Old — still works
   from chaos_jungle.fuzzing import fuzz_scenarios
   results = fuzz_scenarios(fault_pool=pool, workload=wl, target=t, n_combinations=10)

   # New — preferred
   from chaos_jungle.fuzzing import ChaosFuzzer
   results = ChaosFuzzer(fault_pool=pool, target=t).measure(wl, n=10)

----

See also
--------

* :ref:`guide-intercept` — ``ToolMutate``, ``PromptInjection``, per-call targeting
* :ref:`guide-conversation` — multi-turn conversation fuzzing
* :ref:`guide-measurement` — ``ChaosRunner.measure()`` and ``MeasurementResult``
* :ref:`guide-oracles` — defining quality gates for fuzz assertions
