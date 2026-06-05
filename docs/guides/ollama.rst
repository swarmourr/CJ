.. _guide-ollama:

Ollama
======

This guide shows how to use chaos-jungle to test an `Ollama
<https://ollama.ai>`_ server running locally.  All LLM fault types are
demonstrated, including semantic faults and quality measurement with
``LLMJudge``.

.. note::

   **macOS / IPv6 note** — always use ``http://127.0.0.1:11434`` (explicit
   IPv4) instead of ``http://localhost:11434`` when passing the ``upstream``
   parameter.  On macOS, ``localhost`` can resolve to ``::1`` (IPv6) while
   Ollama only binds on IPv4, causing proxy forward failures.

   **Cloud models** — models with ``cloud`` in their tag (e.g.
   ``glm-4.6:cloud``) require external network access and will hang or fail
   when used with the chaos proxy.  Use only locally-running models.

Prerequisites
-------------

.. code-block:: bash

   # Start Ollama
   ollama serve

   # Pull at least one local model
   ollama pull llama3.2
   ollama pull mistral    # optional — useful for multi-model comparisons

   # Install chaos-jungle
   pip install chaos-jungle openai

Running the system test
-----------------------

The bundled test script exercises all fault types against a live Ollama
server and records metrics for each scenario:

.. code-block:: bash

   python3 test/ollamtest.Py
   python3 test/ollamtest.Py --model mistral
   python3 test/ollamtest.Py --model llama3.2 --host http://localhost:11434

The script runs 10 fault tests in sequence.  After all tests finish, export
the results:

.. code-block:: bash

   chaos-jungle export --format csv --dir ./results/ --split

Ollama-specific metrics
-----------------------

Define metrics that extract Ollama's native performance statistics using
the ``@metric`` decorator:

.. code-block:: python

   import json
   import urllib.request
   from chaos_jungle.metrics import metric, Metric

   OLLAMA_HOST  = "http://localhost:11434"
   OLLAMA_MODEL = "llama3.2"
   PROMPT       = "In one sentence, what is chaos engineering?"

   def _ollama_perf_fn(_) -> dict:
       """Collect token speed and duration stats from Ollama's /api/chat."""
       url  = OLLAMA_HOST + "/api/chat"
       body = json.dumps({
           "model":    OLLAMA_MODEL,
           "stream":   False,
           "messages": [{"role": "user", "content": PROMPT}],
       }).encode()
       req = urllib.request.Request(
           url, data=body,
           headers={"Content-Type": "application/json"}, method="POST",
       )
       with urllib.request.urlopen(req, timeout=60) as resp:
           data = json.loads(resp.read())

       eval_count   = data.get("eval_count", 0) or 0
       eval_dur_ns  = data.get("eval_duration", 1) or 1
       total_dur_ns = data.get("total_duration", 0) or 0
       load_dur_ns  = data.get("load_duration", 0) or 0
       prompt_tok   = data.get("prompt_eval_count", 0) or 0

       return {
           "tokens_per_s":     round(eval_count / (eval_dur_ns / 1e9), 2),
           "total_duration_s": round(total_dur_ns / 1e9, 3),
           "eval_count":       eval_count,
           "prompt_tokens":    prompt_tok,
           "load_duration_s":  round(load_dur_ns / 1e9, 4),
       }

   ollama_perf: Metric = metric("ollama")(_ollama_perf_fn)

The metrics are automatically prefixed with the name (``"ollama"``), so the
CSV will contain columns like:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Column
     - Description
   * - ``baseline_ollama_tokens_per_s``
     - Token generation speed before fault
   * - ``chaos_ollama_tokens_per_s``
     - Token generation speed under fault
   * - ``baseline_ollama_total_duration_s``
     - End-to-end wall time reported by Ollama (baseline)
   * - ``baseline_ollama_eval_count``
     - Number of tokens generated (baseline)
   * - ``baseline_ollama_prompt_tokens``
     - Input prompt tokens (baseline)
   * - ``baseline_ollama_load_duration_s``
     - Model cold-load time, 0 if already cached

Fault-by-fault walkthrough
--------------------------

LLMLatency — slow model response
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults.llm import LLMLatency
   from chaos_jungle.targets import LocalTarget

   fault  = LLMLatency(delay_s=2.0, upstream="http://localhost:11434", port=18200)
   runner = ChaosRunner(Scenario("ollama-latency", [fault]), LocalTarget())
   runner.start()

   # Your agent calls go here — they route through the proxy on port 18200
   reply, elapsed = ollama_chat(prompt, "http://127.0.0.1:18200")

   runner.record_result({"elapsed_s": elapsed, "delay_injected_s": 2.0})
   runner.stop()

**What it tests:** timeout budgets, retry compounding, user-visible
progress indicators.

LLMRateLimit — 429 throttle
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = LLMRateLimit(n=2, upstream="http://localhost:11434", port=18201)

Allows the first ``n`` requests through, then returns HTTP 429 for every
subsequent call.

**What it tests:** exponential back-off logic, request queuing,
``Retry-After`` handling.

LLMTimeout — hanging connection
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = LLMTimeout(timeout_s=3.0, upstream="http://localhost:11434", port=18202)

Hangs the connection for ``timeout_s`` seconds, then returns HTTP 504.
The request is never forwarded to Ollama.

**What it tests:** client-side timeout values, task cancellation, deadlock
detection.

LLMResponseCorrupt — malformed JSON
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = LLMResponseCorrupt(mode="truncate", upstream="http://localhost:11434", port=18203)
   fault = LLMResponseCorrupt(mode="empty",    upstream="http://localhost:11434", port=18204)
   fault = LLMResponseCorrupt(mode="invalid_json", upstream="http://localhost:11434", port=18205)

Forwards the real Ollama call but mangles the response body.

**What it tests:** ``JSONDecodeError`` handling, downstream data
propagation, resilience to empty responses.

LLMUnavailable — 503 outage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = LLMUnavailable(upstream="http://localhost:11434", port=18206)

Always returns HTTP 503.  Ollama is never contacted.

**What it tests:** fallback model selection, graceful degradation, user
error messages.

ToolFault — tool-call rejection
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = ToolFault(upstream="http://localhost:11434", port=18207)

Intercepts requests that contain a ``role: "tool"`` message and injects
an HTTP 400 error, simulating a failed tool execution.

**What it tests:** tool-failure recovery in agent loops, whether the agent
retries or propagates the error.

LLMHallucination — wrong answer injection
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = LLMHallucination(
       inject_text="Chaos engineering means making things deliberately worse.",
       upstream="http://localhost:11434",
       port=18208,
   )

Forwards the real Ollama call but replaces the response content with
``inject_text`` before returning it.  Works with both OpenAI format
(``choices[0].message.content``) and Ollama native format
(``message.content``).

**What it tests:** downstream validation layers, fact-checking,
silent wrong-answer propagation.

LLMStreamInterrupt — truncated SSE stream
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = LLMStreamInterrupt(interrupt_after=2, upstream="http://localhost:11434", port=18209)

For streaming requests (``"stream": true``), pipes SSE events back to
the client and then abruptly closes the connection after
``interrupt_after`` data events.

**What it tests:** partial-response handling, streaming error recovery,
incomplete tool-call detection.

LLMTokenStarvation — truncated output
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   fault = LLMTokenStarvation(max_tokens=5, upstream="http://localhost:11434", port=18210)

Rewrites every request to set ``num_predict`` (Ollama) and ``max_tokens``
(OpenAI) to ``max_tokens`` before forwarding.  The model returns a real
but very short response with ``done_reason: "length"``.

**What it tests:** truncated-answer handling, agents that loop when a
response is cut off.

Complete example
----------------

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults.llm import LLMLatency, LLMRateLimit, LLMUnavailable
   from chaos_jungle.metrics import Metric, metric
   from chaos_jungle.targets import LocalTarget
   import json, urllib.request

   HOST  = "http://localhost:11434"
   MODEL = "llama3.2"

   def _perf(_) -> dict:
       body = json.dumps({"model": MODEL, "stream": False,
                          "messages": [{"role": "user", "content": "Hi"}]}).encode()
       req = urllib.request.Request(HOST + "/api/chat", data=body,
                                    headers={"Content-Type": "application/json"})
       with urllib.request.urlopen(req, timeout=30) as r:
           d = json.loads(r.read())
       tps = d.get("eval_count", 0) / max(d.get("eval_duration", 1) / 1e9, 1e-9)
       return {"tokens_per_s": round(tps, 2)}

   perf: Metric = metric("ollama")(_perf)

   baseline = perf.collect(LocalTarget())

   for FaultCls, kwargs, port in [
       (LLMLatency,    {"delay_s": 2.0},  18200),
       (LLMRateLimit,  {"n": 3},          18201),
       (LLMUnavailable, {},               18202),
   ]:
       fault  = FaultCls(upstream=HOST, port=port, **kwargs)
       runner = ChaosRunner(Scenario(fault.name, [fault]), LocalTarget())
       runner.start()

       # run your workload here
       chaos = perf.collect(LocalTarget())

       runner.record_result({
           **{f"baseline_{k}": v for k, v in baseline.items()},
           **{f"chaos_{k}":    v for k, v in chaos.items()},
       })
       runner.stop()
       print(f"{fault.name}: {baseline['ollama_tokens_per_s']} → {chaos['ollama_tokens_per_s']} tok/s")

SemanticCorrupt — AI-layer fault injection
------------------------------------------

``SemanticCorrupt`` mutates the *content* of the LLM request — not the
transport.  The HTTP call succeeds and the JSON is valid, but the context
the model sees is wrong.

.. code-block:: python

   from chaos_jungle.faults.llm import SemanticCorrupt

   # Swap named entities in the context (Paris → Berlin)
   fault = SemanticCorrupt(
       mode="entity_swap",
       upstream="http://127.0.0.1:11434",  # use 127.0.0.1, not localhost
   )

   # Poison a RAG chunk with a false statement
   fault = SemanticCorrupt(
       mode="rag_poison",
       upstream="http://127.0.0.1:11434",
       rag_poison_text="All model outputs are zero.",
   )

See :ref:`guide-semantic` for the full reference.

Quality measurement with LLMJudge
-----------------------------------

Pair ``SemanticCorrupt`` with ``LLMJudge`` to automatically score whether
Ollama's answers degraded under fault:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults.llm import SemanticCorrupt
   from chaos_jungle.judge import LLMJudge
   from chaos_jungle.targets import LocalTarget
   import openai

   # Use Ollama as both the tested model and the judge model
   judge  = LLMJudge(
       model="llama3.2",
       base_url="http://127.0.0.1:11434/v1",
       api_key="ollama",
   )
   fault  = SemanticCorrupt(mode="entity_swap", upstream="http://127.0.0.1:11434")
   runner = ChaosRunner(Scenario("ollama-semantic", [fault]), LocalTarget())

   def workload():
       client = openai.OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama")
       resp = client.chat.completions.create(
           model="llama3.2",
           messages=[
               {"role": "system", "content": "Answer only from the context."},
               {"role": "user",   "content": "Context: Paris is the capital of France.\nQ: What is the capital?"},
           ],
       )
       return {
           "question": "What is the capital of France?",
           "context":  "Paris is the capital of France.",
           "response": resp.choices[0].message.content or "",
       }

   result = runner.measure(workload, n_baseline=3, n_fault=3, evaluator=judge)
   print(result.summary())
   print("Resilient:", result.passed_quality())

See :ref:`guide-judge` for the full evaluator reference.

Exporting results
-----------------

After running your experiments, export all sessions to CSV:

.. code-block:: bash

   # All sessions, one combined file
   chaos-jungle export --format csv

   # Sessions 1–10, one file per session, into ./results/
   chaos-jungle export --sessions 1-10 --dir ./results/ --split

   # Specific sessions
   chaos-jungle export --sessions 1,3,5-8 --dir ./results/

Each row contains ``baseline_*``, ``chaos_*``, and ``delta_*`` columns for
every metric key, plus scenario metadata (name, duration, fault parameters).

See also
--------

* :ref:`guide-semantic` — full SemanticCorrupt reference
* :ref:`guide-judge` — LLMJudge quality evaluator
* :ref:`guide-state` — Redis, JSON, and PostgreSQL state-layer faults
