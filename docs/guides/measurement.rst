.. _guide-measurement:

Measurement
===========

chaos-jungle turns fault injection into a **quantitative** experiment.
``runner.measure()`` runs your workload under both baseline and fault
conditions, computes the delta automatically, and optionally scores AI
output quality with ``LLMJudge``.

----

How measure() works
---------------------

.. mermaid::

   flowchart TD
       MEAS_M["runner.measure(workload, n_baseline=5, n_fault=5)"]
       B1_M["Phase 1: Baseline\nworkload() × n_baseline\nno fault active"]
       B2_M["Phase 2: Fault\nworkload() × n_fault\nfault injected"]
       B3_M["Average each metric across trials"]
       B4_M["Compute delta =\nfault_mean − baseline_mean"]
       B5_M["Score responses with LLMJudge\n(optional)"]
       B6_M["Persist to session DB"]
       RES_M["MeasurementResult"]

       MEAS_M --> B1_M --> B2_M --> B3_M --> B4_M --> B5_M --> B6_M --> RES_M

Your workload must be a zero-argument callable that returns a ``dict``:

.. code-block:: python

   def workload() -> dict:
       t0 = time.time()
       call_my_service()
       return {"duration_s": round(time.time()-t0, 2), "success": 1}

----

Infrastructure faults — latency example
-----------------------------------------

.. code-block:: python

   import time, requests
   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")
   runner = ChaosRunner(
       Scenario("net-delay", [NetworkDelay("200ms", jitter="20ms")]),
       target,
   )

   def workload():
       t0 = time.time()
       r  = requests.get("http://10.0.0.5:8080/api/ping", timeout=5.0)
       return {
           "duration_s": round(time.time()-t0, 2),
           "success":    int(r.status_code == 200),
       }

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())

Output::

   Scenario : net-delay
   Trials   : 5 baseline / 5 fault

     duration_s    baseline=0.012   fault=0.214   Δ +0.202
     success       baseline=1.0     fault=1.0     Δ  0.000

Pass/fail assertion::

   assert result.passed("duration_s", threshold=0.25), (
       f"Latency delta too high: {result.delta['duration_s']:.3f}s"
   )

----

LLM API faults — latency & rate limit
---------------------------------------

.. code-block:: python

   import os, time
   os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:11434/v1"
   os.environ["OPENAI_API_KEY"]  = "ollama"

   import openai
   from chaos_jungle import ChaosRunner, Scenario, LLMLatency, LocalTarget

   runner = ChaosRunner(
       Scenario("llm-latency", [
           LLMLatency(delay_s=3.0, port=18001, upstream="http://127.0.0.1:11434")
       ]),
       LocalTarget(),
   )

   def workload():
       t0 = time.time()
       try:
           openai.OpenAI().chat.completions.create(
               model="qwen2.5:latest",
               messages=[{"role": "user", "content": "What is 2+2?"}],
               timeout=10.0,
           )
           return {"success": 1, "duration_s": round(time.time()-t0, 2)}
       except Exception:
           return {"success": 0, "duration_s": round(time.time()-t0, 2)}

   result = runner.measure(workload, n_baseline=3, n_fault=3)
   print(result.summary())

   # Assert timeout fires correctly
   assert result.fault_mean("duration_s") >= 3.0, "Fault did not add delay"
   assert result.fault_mean("duration_s") <= 6.0, "App did not enforce timeout"

----

LLM quality measurement with LLMJudge
----------------------------------------

Pass ``evaluator=judge`` to score AI output quality on every trial.
The judge scores each response on four dimensions and the delta is included
in the result.

.. code-block:: python

   import os, openai
   os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:11434/v1"
   os.environ["OPENAI_API_KEY"]  = "ollama"

   from chaos_jungle import ChaosRunner, Scenario, SemanticCorrupt, LLMJudge, LocalTarget

   CONTEXT  = "France is in Western Europe. Its capital is Paris."
   QUESTION = "What is the capital of France?"

   judge  = LLMJudge(model="qwen2.5:latest")
   fault  = SemanticCorrupt(mode="entity_swap", port=18050,
                            upstream="http://127.0.0.1:11434")
   runner = ChaosRunner(Scenario("entity-swap", [fault]), LocalTarget())

   def workload():
       resp = openai.OpenAI().chat.completions.create(
           model="qwen2.5:latest",
           messages=[
               {"role": "system", "content": "Answer ONLY from the context."},
               {"role": "user",   "content": f"Context: {CONTEXT}\nQuestion: {QUESTION}"},
           ],
       )
       return {
           "question": QUESTION,
           "context":  CONTEXT,
           "response": resp.choices[0].message.content or "",
       }

   result = runner.measure(workload, n_baseline=3, n_fault=3, evaluator=judge)
   print(result.summary())

Output::

   Scenario : entity-swap
   Trials   : 3 baseline / 3 fault

     faithfulness       baseline=0.95   fault=0.18   Δ -0.77
     hallucination      baseline=0.02   fault=0.91   Δ +0.89
     coherence          baseline=0.94   fault=0.88   Δ -0.06
     guardrail_violation baseline=False fault=False

Quality gate::

   assert result.passed_quality(min_faithfulness=0.70, max_hallucination=0.30), \
       "Quality gate failed — model was misled by entity swap"


----

MeasurementResult — full API
------------------------------

.. code-block:: python

   result.baseline            # {"duration_s": 0.012, "success": 1.0, ...}  (averages)
   result.fault               # {"duration_s": 3.214, "success": 0.8, ...}
   result.delta               # {"duration_s": +3.202, "success": -0.2, ...}
   result.raw_baseline        # list of per-trial dicts (unaveraged)
   result.raw_fault           # list of per-trial dicts (unaveraged)
   result.session_id          # DB session id — link to dashboard / export

   # Convenience accessors
   result.baseline_mean("duration_s")   # 0.012
   result.fault_mean("duration_s")      # 3.214

   # Pass/fail
   result.passed("duration_s", threshold=0.5)      # True if |delta| ≤ 0.5
   result.passed_quality(                           # LLMJudge gate
       min_faithfulness=0.70,
       max_hallucination=0.30,
   )

   result.summary()   # formatted text table (print-ready)

----

CI/CD quality gates
---------------------

Use ``passed()`` and ``passed_quality()`` as pipeline assertions:

.. code-block:: python

   import sys
   from chaos_jungle import ChaosRunner, Scenario, LLMJudge, SemanticCorrupt, LocalTarget
   import openai

   judge  = LLMJudge(model="qwen2.5:latest")
   runner = ChaosRunner(
       Scenario("ci-quality", [SemanticCorrupt(mode="entity_swap", port=18050,
                                               upstream="http://127.0.0.1:11434")]),
       LocalTarget(),
   )

   def workload():
       resp = openai.OpenAI().chat.completions.create(
           model="qwen2.5:latest",
           messages=[
               {"role": "system", "content": "Answer ONLY from the context."},
               {"role": "user",   "content": "Context: Paris is the capital of France.\nQuestion: What is the capital of France?"},
           ],
       )
       return {"question": "...", "context": "...",
               "response": resp.choices[0].message.content or ""}

   result = runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)
   print(result.summary())

   if not result.passed_quality(min_faithfulness=0.70, max_hallucination=0.30):
       print("FAIL: quality gate not met")
       sys.exit(1)

   print("PASS: quality gate met")
   sys.exit(0)

Recommended quality thresholds by risk tier:

.. list-table::
   :header-rows: 1
   :widths: 30 25 25 20

   * - Risk tier
     - ``min_faithfulness``
     - ``max_hallucination``
     - ``duration_max``
   * - Low (internal tool)
     - 0.60
     - 0.40
     - 10 s
   * - Medium (customer-facing)
     - 0.75
     - 0.25
     - 5 s
   * - High (medical / legal / financial)
     - 0.90
     - 0.10
     - 2 s

----

Fault scheduling — inject mid-workload
----------------------------------------

``start(start_after=N)`` defers injection by N seconds.  Simulates a failure
that occurs *during* a computation, not before it starts:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, LocalTarget

   runner = ChaosRunner(
       Scenario("mid-job-fault", [NetworkDelay("2000ms")]),
       LocalTarget(),
   )

   # Inject at T=30s, auto-clear at T=45s
   runner.start(start_after=30, duration=15)
   run_long_computation()   # fault hits mid-run without any code change
   runner.stop()            # safe to call even after auto-stop

Fault window::

   T=0s    workload starts
   T=30s   fault injected  (start_after=30)
   T=45s   fault cleared   (duration=15)
   T=60s   workload ends

----

Fault composition — compounding effects
-----------------------------------------

Combine multiple faults and measure their compounding impact:

.. code-block:: python

   from chaos_jungle import (
       ChaosRunner, Scenario, LocalTarget,
       NetworkDelay, CPUStress, LLMLatency,
   )
   import time, requests

   def workload():
       t0 = time.time()
       r  = requests.get("http://localhost:8080/api/infer", timeout=10.0)
       return {"duration_s": round(time.time()-t0, 2), "success": int(r.ok)}

   def run(label, faults):
       return ChaosRunner(
           Scenario(label, faults), LocalTarget()
       ).measure(workload, n_baseline=3, n_fault=3)

   r_net  = run("network-only",  [NetworkDelay("200ms")])
   r_cpu  = run("cpu-only",      [CPUStress(cores=2)])
   r_both = run("combined",      [NetworkDelay("200ms"), CPUStress(cores=2)])

   print(f"network Δduration: {r_net.delta['duration_s']:+.3f}s")
   print(f"cpu     Δduration: {r_cpu.delta['duration_s']:+.3f}s")
   print(f"combined Δduration: {r_both.delta['duration_s']:+.3f}s  ← compounding")

----

Summary table
--------------

.. list-table::
   :header-rows: 1
   :widths: 35 35 30

   * - Feature
     - API
     - Use case
   * - Automatic measurement
     - ``runner.measure(workload, n_baseline, n_fault)``
     - Quantify fault impact with statistics
   * - LLM quality scoring
     - ``runner.measure(..., evaluator=judge)``
     - Faithfulness / hallucination delta
   * - Pass/fail — numeric metric
     - ``result.passed(key, threshold)``
     - CI/CD latency / error gate
   * - Pass/fail — AI quality
     - ``result.passed_quality(min_faithfulness, max_hallucination)``
     - CI/CD quality gate
   * - Fault scheduling
     - ``runner.start(start_after=N, duration=M)``
     - Inject fault mid-computation
   * - Fault composition
     - ``Scenario("name", [fault1, fault2])``
     - Measure compounding effects
   * - Raw trial data
     - ``result.raw_baseline`` / ``result.raw_fault``
     - Statistical analysis, plotting
   * - DB persistence
     - ``result.session_id``
     - Dashboard, ``chaos-jungle export``

See also
---------

* :ref:`guide-judge` — LLMJudge evaluator API
* :ref:`guide-metrics` — built-in and custom Metric classes
* :ref:`guide-llm` — LLM API fault parameters
